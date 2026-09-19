package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	v1 "github.com/Wei-Shaw/sub2api/pkg/pluginapi/v1"
	v2 "github.com/Wei-Shaw/sub2api/pkg/pluginapi/v2"
	"google.golang.org/grpc"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const pluginID = "local.flownode.state-reuse"
const version = "1.0.13"
const stateHeader = "X-Codex-Turn-State"
const dataDir = "/app/data/fn-state-reuse"
const businessConcurrencyPerAccount = 2

type Config struct {
	ProxyURL        string  `json:"proxy_url"`
	HarvestProxyAPI string  `json:"harvest_proxy_api,omitempty"`
	Accounts        []int64 `json:"accounts"`
	Suspended       []int64 `json:"suspended"`
}
type Ticket struct {
	AccountID      int64  `json:"account_id"`
	Model          string `json:"model"`
	CredentialHash string `json:"credential_hash"`
	Value          string `json:"value"`
	Issued         int64  `json:"issued"`
}
type Entry struct {
	mu       sync.Mutex
	ticket   Ticket
	cooldown time.Time
	blocked  bool
}
type Plugin struct {
	mu                    sync.Mutex
	config                Config
	entries               map[string]*Entry
	client                *http.Client
	businessClientFactory func(string) (*http.Client, error)
	businessSlots         map[int64]chan struct{}
	businessBackoff       map[int64]time.Time
	store                 string
	slots                 chan struct{}
	reused                int
	harvested             int
	rejected              int
}

func NewPlugin(store string) *Plugin {
	return &Plugin{entries: map[string]*Entry{}, businessSlots: map[int64]chan struct{}{}, businessBackoff: map[int64]time.Time{}, store: store, slots: make(chan struct{}, 3)}
}
func capability() v2.Capability {
	return v2.Capability{ID: v2.CapabilityProtectionTransport, Kind: v2.CapabilityKindProvider, Platform: "openai", AccountType: "oauth", Permissions: []v2.Permission{v2.PermissionRequestMetadata, v2.PermissionRequestBody, v2.PermissionCredentialsForward, v2.PermissionNetworkOutbound, v2.PermissionAccountProtection, v2.PermissionOriginalRequest}, TimeoutMS: 120000, FailureMode: v2.FailureModeClosed, Synchronous: true}
}
func (p *Plugin) GetInfo(context.Context) (v2.PluginInfo, error) {
	return v2.PluginInfo{PluginID: pluginID, PluginVersion: version, ProtocolVersion: 2, Capabilities: []v2.Capability{capability()}}, nil
}
func (p *Plugin) Health(context.Context) (v2.HealthStatus, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return v2.HealthStatus{Healthy: true, Message: fmt.Sprintf("state reuse ready; injected=%d harvested=%d rejected=%d", p.reused, p.harvested, p.rejected)}, nil
}
func parseConfig(raw []byte) (Config, error) {
	var c Config
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(&c); err != nil {
		return c, errors.New("invalid config")
	}
	if d.Decode(new(any)) != io.EOF {
		return c, errors.New("trailing config")
	}
	if c.ProxyURL == "" {
		return c, errors.New("proxy_url is required; use an endpoint reachable from the plugin container")
	}
	u, e := url.Parse(c.ProxyURL)
	if e != nil || (u.Scheme != "http" && u.Scheme != "https" && u.Scheme != "socks5" && u.Scheme != "socks5h") || u.Hostname() == "" || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
		return c, errors.New("invalid harvest proxy URL")
	}
	if c.HarvestProxyAPI != "" {
		h, e := url.Parse(c.HarvestProxyAPI)
		if e != nil || h.Scheme != "http" || h.Host == "" || h.User != nil || h.Path != "/gen" || h.Fragment != "" {
			return c, errors.New("invalid harvest proxy generator")
		}
	}
	for _, id := range append(append([]int64{}, c.Accounts...), c.Suspended...) {
		if id <= 0 {
			return c, errors.New("invalid account")
		}
	}
	return c, nil
}

// harvestClient obtains a one-use proxy endpoint for ticket collection only.
// The returned client is never used for business forwarding.
func (p *Plugin) harvestClient(ctx context.Context) (*http.Client, error) {
	p.mu.Lock()
	api := p.config.HarvestProxyAPI
	fallback := p.client
	p.mu.Unlock()
	if api == "" {
		if fallback == nil {
			return nil, errors.New("harvest client unavailable")
		}
		return fallback, nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, api, nil)
	if err != nil {
		return nil, err
	}
	resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(req)
	if err != nil {
		return nil, fmt.Errorf("proxy generator: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("proxy generator HTTP %d", resp.StatusCode)
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 4096))
	if err != nil {
		return nil, err
	}
	line := strings.TrimSpace(strings.Split(string(b), "\n")[0])
	host, port, err := net.SplitHostPort(line)
	if err != nil || net.ParseIP(host) == nil || port == "" {
		return nil, errors.New("proxy generator returned invalid endpoint")
	}
	u := &url.URL{Scheme: "http", Host: net.JoinHostPort(host, port)}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.Proxy = http.ProxyURL(u)
	tr.ResponseHeaderTimeout = 90 * time.Second
	return &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, nil
}
func (p *Plugin) ValidateConfig(_ context.Context, raw json.RawMessage) (json.RawMessage, error) {
	c, e := parseConfig(raw)
	if e != nil {
		return nil, e
	}
	return json.Marshal(c)
}
func (p *Plugin) ApplyConfig(_ context.Context, raw json.RawMessage) error {
	c, e := parseConfig(raw)
	if e != nil {
		return e
	}
	u, _ := url.Parse(c.ProxyURL)
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.Proxy = http.ProxyURL(u)
	tr.ResponseHeaderTimeout = 90 * time.Second
	tr.MaxIdleConns = 64
	tr.MaxIdleConnsPerHost = 32
	client := &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.client != nil {
		p.client.CloseIdleConnections()
	}
	p.config = c
	p.client = client
	if raw, e := os.ReadFile(p.store); e == nil {
		var tickets []Ticket
		if json.Unmarshal(raw, &tickets) != nil {
			return errors.New("invalid persistent ticket store")
		}
		for _, t := range tickets {
			if validTicket(t, time.Now()) {
				k := key(t.AccountID, t.Model, t.CredentialHash)
				if _, ok := p.entries[k]; !ok {
					p.entries[k] = &Entry{ticket: t}
				}
			}
		}
	}
	return nil
}
func (p *Plugin) TestConfig(ctx context.Context, raw json.RawMessage) (time.Duration, error) {
	_, e := p.ValidateConfig(ctx, raw)
	return 0, e
}
func (p *Plugin) Preprocess(context.Context, v2.PreprocessRequest) (v2.PreprocessResponse, error) {
	return v2.PreprocessResponse{Decision: v2.DecisionPass}, nil
}
func digest(s string) string { v := sha256.Sum256([]byte(s)); return hex.EncodeToString(v[:]) }
func identity(h http.Header) string {
	return digest(strings.TrimPrefix(h.Get("Authorization"), "Bearer ") + ":" + h.Get("ChatGPT-Account-Id"))
}
func key(id int64, model, hash string) string { return fmt.Sprintf("%d:%s:%s", id, model, hash) }
func parseState(value string) (int64, bool) {
	if (len(value) != 332 && len(value) != 292) || strings.ContainsAny(value, " \r\n\t") {
		return 0, false
	}
	b, e := base64.URLEncoding.Strict().DecodeString(value)
	if e != nil || !((len(value) == 332 && len(b) == 249) || (len(value) == 292 && len(b) == 217)) || b[0] != 128 {
		return 0, false
	}
	issued := int64(binary.BigEndian.Uint64(b[1:9]))
	return issued, issued >= 1577836800 && issued < 4102444800
}
func validTicket(t Ticket, now time.Time) bool {
	i, ok := parseState(t.Value)
	return ok && i == t.Issued && i <= now.Unix()+30 && now.Unix() < i+3570 && t.AccountID > 0 && t.Model != "" && len(t.CredentialHash) == 64
}
func contains(ids []int64, id int64) bool {
	for _, v := range ids {
		if v == id {
			return true
		}
	}
	return false
}
func (p *Plugin) audit(event string, t Ticket, extra string) {
	row := map[string]any{"at": time.Now().UTC().Format(time.RFC3339), "event": event, "account_id": t.AccountID, "model": t.Model, "ticket_sha256": digest(t.Value)[:16], "issued": t.Issued, "length": len(t.Value), "detail": extra}
	b, _ := json.Marshal(row)
	p.mu.Lock()
	defer p.mu.Unlock()
	f, e := os.OpenFile(filepath.Join(filepath.Dir(p.store), "audit.jsonl"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if e == nil {
		defer f.Close()
		_, _ = f.Write(append(b, '\n'))
	}
}

// Caller owns the entry lock. Persist before publishing the new entry value.
func (p *Plugin) persist(k string, t Ticket) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	var tickets []Ticket
	raw, e := os.ReadFile(p.store)
	if e == nil {
		if json.Unmarshal(raw, &tickets) != nil {
			return errors.New("invalid store")
		}
	} else if !os.IsNotExist(e) {
		return errors.New("store unreadable")
	}
	out := []Ticket{}
	for _, old := range tickets {
		if key(old.AccountID, old.Model, old.CredentialHash) != k && validTicket(old, time.Now()) {
			out = append(out, old)
		}
	}
	if t.Value != "" {
		out = append(out, t)
	}
	b, _ := json.Marshal(out)
	tmp := p.store + ".tmp"
	if e = os.WriteFile(tmp, b, 0600); e != nil {
		return e
	}
	return os.Rename(tmp, p.store)
}

type proxyContextKey struct{}

func withProxy(ctx context.Context, proxy string) context.Context {
	return context.WithValue(ctx, proxyContextKey{}, proxy)
}
func proxyFromContext(ctx context.Context) string {
	v, _ := ctx.Value(proxyContextKey{}).(string)
	return v
}
func (p *Plugin) businessClient(proxyURL string) (*http.Client, error) {
	if p.businessClientFactory != nil {
		return p.businessClientFactory(proxyURL)
	}
	if proxyURL == "" {
		tr := http.DefaultTransport.(*http.Transport).Clone()
		tr.Proxy = nil
		tr.ResponseHeaderTimeout = 90 * time.Second
		return &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, nil
	}
	u, err := url.Parse(proxyURL)
	if err != nil || u.Hostname() == "" || (u.Scheme != "http" && u.Scheme != "https" && u.Scheme != "socks5" && u.Scheme != "socks5h") || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" {
		return nil, errors.New("invalid account proxy")
	}
	tr := http.DefaultTransport.(*http.Transport).Clone()
	tr.Proxy = http.ProxyURL(u)
	tr.ResponseHeaderTimeout = 90 * time.Second
	tr.MaxIdleConns = 64
	tr.MaxIdleConnsPerHost = 32
	return &http.Client{Transport: tr, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}, nil
}

func (p *Plugin) acquireBusiness(ctx context.Context, accountID int64) (func(), error) {
	p.mu.Lock()
	slot := p.businessSlots[accountID]
	if slot == nil {
		slot = make(chan struct{}, businessConcurrencyPerAccount)
		p.businessSlots[accountID] = slot
	}
	p.mu.Unlock()
	select {
	case slot <- struct{}{}:
		return func() { <-slot }, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func (p *Plugin) ticket(ctx context.Context, id int64, model string, headers http.Header) (Ticket, error) {
	hash := identity(headers)
	k := key(id, model, hash)
	p.mu.Lock()
	entry := p.entries[k]
	if entry == nil {
		entry = &Entry{}
		p.entries[k] = entry
	}
	suspended := contains(p.config.Suspended, id)
	p.mu.Unlock()
	if suspended {
		return Ticket{}, errors.New("account suspended after failed authentication")
	}
	entry.mu.Lock()
	defer entry.mu.Unlock()
	now := time.Now()
	// Import verified candidates under the same entry/store locks as automatic harvest.
	candidate := filepath.Join(filepath.Dir(p.store), "incoming", digest(k)+".json")
	if raw, err := os.ReadFile(candidate); err == nil {
		var imported Ticket
		if json.Unmarshal(raw, &imported) == nil && key(imported.AccountID, imported.Model, imported.CredentialHash) == k && validTicket(imported, now) && imported.Issued > entry.ticket.Issued {
			if err := p.persist(k, imported); err != nil {
				return Ticket{}, errors.New("cannot persist imported ticket")
			}
			entry.ticket = imported
			entry.cooldown = time.Time{}
			entry.blocked = false
			p.audit("import", imported, "verified candidate matched current credential and model")
			_ = os.Remove(candidate)
		}
	}
	if entry.blocked {
		return Ticket{}, errors.New("credential denied; wait for new authentication")
	}
	if validTicket(entry.ticket, now) && now.Unix() < entry.ticket.Issued+3000 {
		return entry.ticket, nil
	}
	if now.Before(entry.cooldown) {
		if validTicket(entry.ticket, now) {
			return entry.ticket, nil
		}
		return Ticket{}, errors.New("harvest cooling down")
	}
	select {
	case p.slots <- struct{}{}:
		defer func() { <-p.slots }()
	case <-ctx.Done():
		return Ticket{}, ctx.Err()
	}
	harvestCtx, cancel := context.WithTimeout(ctx, 25*time.Second)
	defer cancel()
	harvestClient, _ := p.harvestClient(harvestCtx)
	if harvestClient == nil {
		entry.cooldown = time.Now().Add(time.Minute)
		return Ticket{}, errors.New("harvest proxy unavailable")
	}
	defer harvestClient.CloseIdleConnections()
	t, terminal, e := harvest(harvestCtx, harvestClient, id, model, headers)
	if e != nil {
		entry.cooldown = time.Now().Add(time.Minute)
		entry.blocked = terminal
		if terminal || strings.Contains(e.Error(), "429") || strings.Contains(e.Error(), "rate_limit") || strings.Contains(e.Error(), "usage_limit") {
			entry.cooldown = time.Now().Add(5 * time.Minute)
		}
		if !terminal && validTicket(entry.ticket, time.Now()) {
			return entry.ticket, nil
		}
		return Ticket{}, e
	}
	if e = p.persist(k, t); e != nil {
		return Ticket{}, errors.New("cannot persist ticket")
	}
	entry.ticket = t
	entry.cooldown = time.Time{}
	p.mu.Lock()
	p.harvested++
	p.mu.Unlock()
	p.audit("harvest", t, "validated 292/332 and requested model")
	return t, nil
}
func harvest(ctx context.Context, client *http.Client, id int64, model string, h http.Header) (Ticket, bool, error) {
	body, _ := json.Marshal(map[string]any{"model": model, "instructions": "Reply with OK.", "input": []any{map[string]any{"type": "message", "role": "user", "content": []any{map[string]string{"type": "input_text", "text": "Reply with OK."}}}}, "stream": true, "store": false, "parallel_tool_calls": true, "include": []string{"reasoning.encrypted_content"}})
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", bytes.NewReader(body))
	for _, name := range []string{"Authorization", "ChatGPT-Account-Id"} {
		req.Header.Set(name, h.Get(name))
	}
	req.Header.Set("User-Agent", "codex_cli_rs/0.155.0")
	req.Header.Set("Version", "0.155.0")
	req.Header.Set("Originator", "codex_cli_rs")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")
	resp, e := client.Do(req)
	if e != nil {
		return Ticket{}, false, errors.New("harvest transport failed")
	}
	defer resp.Body.Close()
	terminal := resp.StatusCode == 401 || resp.StatusCode == 403
	if resp.StatusCode != 200 {
		return Ticket{}, terminal, fmt.Errorf("harvest HTTP %d", resp.StatusCode)
	}
	state := resp.Header.Get(stateHeader)
	issued, shape := parseState(state)
	completed := false
	failed := false
	limited := false
	match := false
	size := 0
	scan := bufio.NewScanner(io.LimitReader(resp.Body, (1<<20)+1))
	scan.Buffer(make([]byte, 4096), 1<<20)
	for scan.Scan() {
		line := scan.Bytes()
		size += len(line) + 1
		if size > 1<<20 {
			return Ticket{}, false, errors.New("harvest response too large")
		}
		if !bytes.HasPrefix(line, []byte("data:")) {
			continue
		}
		var event struct {
			Type  string `json:"type"`
			Code  string `json:"code"`
			Error struct {
				Code string `json:"code"`
			} `json:"error"`
			Response struct {
				Model string `json:"model"`
				Error struct {
					Code string `json:"code"`
				} `json:"error"`
			} `json:"response"`
		}
		if json.Unmarshal(bytes.TrimSpace(line[5:]), &event) != nil {
			continue
		}
		switch event.Type {
		case "response.completed":
			completed = true
			match = event.Response.Model == model
		case "response.failed", "response.incomplete", "error":
			failed = true
			code := event.Error.Code + event.Response.Error.Code + event.Code
			limited = limited || strings.Contains(code, "usage_limit") || strings.Contains(code, "rate_limit") || strings.Contains(code, "insufficient_quota")
			if strings.Contains(code, "invalid_api_key") || strings.Contains(code, "token_expired") || strings.Contains(code, "account_deactivated") {
				terminal = true
			}
		}
	}
	t := Ticket{id, model, identity(h), state, issued}
	if scan.Err() != nil || !completed || failed || !match || !shape || !validTicket(t, time.Now()) {
		if limited {
			return Ticket{}, terminal, errors.New("harvest usage_limit or rate_limit")
		}
		return Ticket{}, terminal, fmt.Errorf("no complete model-matched 292/332 ticket: length=%d shape=%t completed=%t failed=%t model_matches=%t", len(state), shape, completed, failed, match)
	}
	return t, false, nil
}
func errorFrame(s grpc.BidiStreamingServer[v1.ForwardRequest, v1.ForwardResponse], code string, sent bool) error {
	return s.Send(&v1.ForwardResponse{Frame: &v1.ForwardResponse_Error{Error: &v1.ForwardResponseError{Code: code, Message: "STATE-protected request unavailable; no unprotected fallback", RequestSent: sent}}})
}
func (p *Plugin) Forward(stream grpc.BidiStreamingServer[v1.ForwardRequest, v1.ForwardResponse]) error {
	f, e := stream.Recv()
	if e != nil {
		return e
	}
	start := f.GetStart()
	if start == nil {
		return errorFrame(stream, "invalid_start", false)
	}
	fail := func(code string, sent bool) error {
		p.audit("transport_error", Ticket{AccountID: start.AccountId}, fmt.Sprintf("code=%s sent=%t", code, sent))
		return errorFrame(stream, code, sent)
	}
	u, e := url.Parse(start.Url)
	if e != nil || u.Scheme != "https" || u.Host != "chatgpt.com" || !strings.HasPrefix(u.Path, "/backend-api/codex/") {
		return fail("invalid_upstream", false)
	}
	var body bytes.Buffer
	for {
		f, e = stream.Recv()
		if e != nil {
			return e
		}
		if f.GetBodyEnd() {
			break
		}
		chunk := f.GetBodyChunk()
		if body.Len()+len(chunk) > 256<<20 {
			p.audit("request_rejected", Ticket{AccountID: start.AccountId}, "HTTP 413 body exceeds host 256 MiB limit")
			if err := stream.Send(&v1.ForwardResponse{Frame: &v1.ForwardResponse_Start{Start: &v1.ForwardResponseStart{StatusCode: 413, Status: "413 Request Entity Too Large", Protocol: "HTTP/1.1", ProtocolMajor: 1, ProtocolMinor: 1, ContentLength: 0}}}); err != nil {
				return err
			}
			return stream.Send(&v1.ForwardResponse{Frame: &v1.ForwardResponse_End{End: &v1.ForwardResponseEnd{}}})
		}
		body.Write(chunk)
	}
	req, e := http.NewRequestWithContext(stream.Context(), start.Method, start.Url, bytes.NewReader(body.Bytes()))
	if e != nil {
		return fail("invalid_request", false)
	}
	for k, v := range start.Headers {
		req.Header[k] = append([]string{}, v.Values...)
	}
	req.Host = start.Host
	var metadata struct {
		Model string `json:"model"`
	}
	if len(body.Bytes()) > 0 && json.Unmarshal(body.Bytes(), &metadata) != nil {
		return fail("invalid_body", false)
	}
	p.mu.Lock()
	enabled := contains(p.config.Accounts, start.AccountId)
	client := p.client
	p.mu.Unlock()
	var t Ticket
	protected := enabled && start.Method == "POST" && (strings.HasSuffix(u.Path, "/responses") || strings.HasSuffix(u.Path, "/responses/compact"))
	if !enabled {
		return fail("account_outside_scope", false)
	}
	releaseBusiness, waitErr := p.acquireBusiness(stream.Context(), start.AccountId)
	if waitErr != nil {
		return fail("account_concurrency_wait_canceled", false)
	}
	defer releaseBusiness()
	p.mu.Lock()
	until := p.businessBackoff[start.AccountId]
	p.mu.Unlock()
	if time.Now().Before(until) {
		seconds := int(time.Until(until).Seconds()) + 1
		if err := stream.Send(&v1.ForwardResponse{Frame: &v1.ForwardResponse_Start{Start: &v1.ForwardResponseStart{StatusCode: 429, Status: "429 Too Many Requests", Protocol: "HTTP/1.1", ProtocolMajor: 1, ProtocolMinor: 1, Headers: map[string]*v1.HeaderValues{"Retry-After": {Values: []string{strconv.Itoa(seconds)}}}}}}); err != nil {
			return err
		}
		return stream.Send(&v1.ForwardResponse{Frame: &v1.ForwardResponse_End{End: &v1.ForwardResponseEnd{}}})
	}
	if protected {
		if metadata.Model == "" {
			return fail("missing_model", false)
		}
		if metadata.Model == "gpt-6-astra" {
			t, e = p.ticket(withProxy(stream.Context(), start.ProxyUrl), start.AccountId, metadata.Model, req.Header)
		} else {
			p.mu.Lock()
			entry := p.entries[key(start.AccountId, metadata.Model, identity(req.Header))]
			p.mu.Unlock()
			if entry != nil {
				entry.mu.Lock()
				if validTicket(entry.ticket, time.Now()) {
					t = entry.ticket
				}
				entry.mu.Unlock()
			}
			if t.Value == "" {
				e = errors.New("no cached model ticket; availability mode")
			}
		}
		if e != nil {
			p.mu.Lock()
			p.rejected++
			p.mu.Unlock()
			p.audit("reject", Ticket{AccountID: start.AccountId, Model: metadata.Model}, e.Error())
			// Missing tickets affect group classification, not request availability.
			protected = false
		}
		if protected {
			req.Header.Set(stateHeader, t.Value)
			p.mu.Lock()
			p.reused++
			p.mu.Unlock()
			p.audit("inject", t, "account/model/credential matched")
		} else {
			req.Header.Del(stateHeader)
			p.audit("unprotected_forward", Ticket{AccountID: start.AccountId, Model: metadata.Model}, "no valid ticket; forward normally")
		}
	}

	// No automatic retries and no redirect following after the business request is sent.
	began := time.Now()
	businessClient, ce := p.businessClient(start.ProxyUrl)
	if ce != nil {
		return fail("invalid_account_proxy", false)
	}
	resp, e := businessClient.Do(req)
	if businessClient != client {
		businessClient.CloseIdleConnections()
	}
	if e != nil {
		p.audit("network_error", t, fmt.Sprintf("type=%T canceled=%t timeout=%t", e, stream.Context().Err() != nil, isTimeout(e)))
		return fail("upstream_transport_error", true)
	}
	defer resp.Body.Close()
	if resp.StatusCode == 429 {
		until := time.Now().Add(5 * time.Minute)
		if seconds, err := strconv.Atoi(resp.Header.Get("Retry-After")); err == nil && seconds > 0 {
			candidate := time.Now().Add(time.Duration(seconds) * time.Second)
			if candidate.After(until) {
				until = candidate
			}
		} else if candidate, err := http.ParseTime(resp.Header.Get("Retry-After")); err == nil && candidate.After(until) {
			until = candidate
		}
		p.mu.Lock()
		if until.After(p.businessBackoff[start.AccountId]) {
			p.businessBackoff[start.AccountId] = until
		}
		p.mu.Unlock()
	}
	hdr := map[string]*v1.HeaderValues{}
	for k, v := range resp.Header {
		hdr[k] = &v1.HeaderValues{Values: v}
	}
	if protected {
		hdr["X-Flow-State-Reused"] = &v1.HeaderValues{Values: []string{fmt.Sprint(len(t.Value))}}
		p.audit("response", t, fmt.Sprintf("HTTP %d", resp.StatusCode))
	}
	if protected && (resp.StatusCode == 401 || resp.StatusCode == 403 || resp.StatusCode == 429) {
		p.mu.Lock()
		entry := p.entries[key(t.AccountID, t.Model, t.CredentialHash)]
		p.mu.Unlock()
		if entry != nil {
			entry.mu.Lock()
			if entry.ticket.Value == t.Value && entry.ticket.Issued == t.Issued {
				entry.cooldown = time.Now().Add(5 * time.Minute)
				if resp.StatusCode != 429 {
					_ = p.persist(key(t.AccountID, t.Model, t.CredentialHash), Ticket{})
					entry.ticket = Ticket{}
					entry.blocked = true
				}
			}
			entry.mu.Unlock()
		}
	}
	if e = stream.Send(&v1.ForwardResponse{Frame: &v1.ForwardResponse_Start{Start: &v1.ForwardResponseStart{StatusCode: int32(resp.StatusCode), Status: resp.Status, Protocol: resp.Proto, ProtocolMajor: int32(resp.ProtoMajor), ProtocolMinor: int32(resp.ProtoMinor), Headers: hdr, ContentLength: resp.ContentLength}}}); e != nil {
		return e
	}
	buf := make([]byte, 32*1024)
	var nread int64
	for {
		n, err := resp.Body.Read(buf)
		if n > 0 {
			nread += int64(n)
			if e = stream.Send(&v1.ForwardResponse{Frame: &v1.ForwardResponse_BodyChunk{BodyChunk: append([]byte{}, buf[:n]...)}}); e != nil {
				return e
			}
		}
		if err == io.EOF {
			break
		}
		if err != nil {
			return fail("response_read_error", true)
		}
	}
	return stream.Send(&v1.ForwardResponse{Frame: &v1.ForwardResponse_End{End: &v1.ForwardResponseEnd{BytesReceived: nread, DurationMs: time.Since(began).Milliseconds()}}})
}
func isTimeout(err error) bool {
	var e interface{ Timeout() bool }
	return errors.As(err, &e) && e.Timeout()
}
func main() { v2.Serve(NewPlugin(filepath.Join(dataDir, "tickets.json"))) }
