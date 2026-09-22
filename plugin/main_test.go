package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	v1 "github.com/Wei-Shaw/sub2api/pkg/pluginapi/v1"
	"google.golang.org/grpc/metadata"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type roundtrip func(*http.Request) (*http.Response, error)

func (f roundtrip) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }
func makeTicket(id int64, model, hash string, issued int64) Ticket {
	b := make([]byte, 249)
	b[0] = 128
	binary.BigEndian.PutUint64(b[1:9], uint64(issued))
	return Ticket{AccountID: id, Model: model, CredentialHash: hash, Value: base64.URLEncoding.EncodeToString(b), Issued: issued}
}
func headers() http.Header {
	return http.Header{"Authorization": []string{"Bearer synthetic-key"}, "Chatgpt-Account-Id": []string{"synthetic-account"}}
}
func TestCredentialAndModelIsolationAndPersistence(t *testing.T) {
	h := headers()
	dir := t.TempDir()
	p := NewPlugin(filepath.Join(dir, "tickets.json"))
	ticket := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix())
	b, _ := json.Marshal([]Ticket{ticket})
	os.WriteFile(p.store, b, 0600)
	if e := p.ApplyConfig(context.Background(), []byte(`{"accounts":[17],"proxy_url":"http://127.0.0.1:18300"}`)); e != nil {
		t.Fatal(e)
	}
	var calls atomic.Int32
	p.client = &http.Client{Transport: roundtrip(func(r *http.Request) (*http.Response, error) {
		calls.Add(1)
		return &http.Response{StatusCode: 429, Body: io.NopCloser(bytes.NewBufferString("")), Header: make(http.Header)}, nil
	})}
	got, e := p.ticket(context.Background(), 17, "gpt-6-astra", h)
	if e != nil || got.Value != ticket.Value || calls.Load() != 0 {
		t.Fatal("persisted ticket not reused")
	}
	if _, e = p.ticket(context.Background(), 18, "gpt-6-astra", h); e == nil {
		t.Fatal("cross-account reuse")
	}
	if _, e = p.ticket(context.Background(), 17, "gpt-5.6-sol", h); e == nil {
		t.Fatal("cross-model reuse")
	}
	h.Set("Authorization", "Bearer rotated")
	if _, e = p.ticket(context.Background(), 17, "gpt-6-astra", h); e == nil {
		t.Fatal("cross-credential reuse")
	}
	n := calls.Load()
	p.ticket(context.Background(), 17, "gpt-6-astra", h)
	if calls.Load() != n {
		t.Fatal("429 cooling down must not probe again")
	}
}
func TestConcurrentHarvestAndTerminalSSE(t *testing.T) {
	for _, bad := range []bool{false, true} {
		t.Run(map[bool]string{false: "success", true: "late_failure"}[bad], func(t *testing.T) {
			h := headers()
			p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
			var calls atomic.Int32
			p.client = &http.Client{Transport: roundtrip(func(r *http.Request) (*http.Response, error) {
				calls.Add(1)
				if r.Header.Get(stateHeader) != "" {
					t.Error("harvest carried old state")
				}
				ticket := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix())
				body := "data: {\"type\":\"response.completed\",\"response\":{\"model\":\"gpt-6-astra\"}}\n\n"
				if bad {
					body += "data: {\"type\":\"response.failed\"}\n\n"
				}
				return &http.Response{StatusCode: 200, Header: http.Header{stateHeader: []string{ticket.Value}}, Body: io.NopCloser(bytes.NewBufferString(body))}, nil
			})}
			var wg sync.WaitGroup
			for i := 0; i < 12; i++ {
				wg.Add(1)
				go func() {
					defer wg.Done()
					_, e := p.ticket(context.Background(), 17, "gpt-6-astra", h)
					if (e != nil) != bad {
						t.Error("bad result", e)
					}
				}()
			}
			wg.Wait()
			if calls.Load() != 1 {
				t.Fatalf("harvest requests=%d", calls.Load())
			}
		})
	}
}

type stream struct {
	ctx context.Context
	in  []*v1.ForwardRequest
	out []*v1.ForwardResponse
}

func (s *stream) Context() context.Context         { return s.ctx }
func (s *stream) SetHeader(metadata.MD) error      { return nil }
func (s *stream) SendHeader(metadata.MD) error     { return nil }
func (s *stream) SetTrailer(metadata.MD)           {}
func (s *stream) SendMsg(any) error                { return nil }
func (s *stream) RecvMsg(any) error                { return nil }
func (s *stream) Send(m *v1.ForwardResponse) error { s.out = append(s.out, m); return nil }
func (s *stream) Recv() (*v1.ForwardRequest, error) {
	if len(s.in) == 0 {
		return nil, io.EOF
	}
	f := s.in[0]
	s.in = s.in[1:]
	return f, nil
}
func TestForwardInjectsWithoutChangingBody(t *testing.T) {
	h := headers()
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	ticket := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix())
	p.businessClientFactory = func(string) (*http.Client, error) { return p.client, nil }
	p.config.Accounts = []int64{17}
	p.entries[key(17, ticket.Model, ticket.CredentialHash)] = &Entry{ticket: ticket}
	body := []byte(`{"model":"gpt-6-astra","input":[{"role":"user","content":"hello"}],"stream":true}`)
	p.client = &http.Client{Transport: roundtrip(func(r *http.Request) (*http.Response, error) {
		b, _ := io.ReadAll(r.Body)
		if !bytes.Equal(b, body) {
			t.Error("business body changed")
		}
		if r.Header.Get(stateHeader) != ticket.Value {
			t.Error("not injected")
		}
		return &http.Response{StatusCode: 200, Status: "200 OK", Proto: "HTTP/2.0", ProtoMajor: 2, Header: make(http.Header), Body: io.NopCloser(bytes.NewBufferString("data: {\"type\":\"response.completed\",\"response\":{\"model\":\"gpt-6-astra\"}}\n\n"))}, nil
	})}
	hs := map[string]*v1.HeaderValues{}
	for k, v := range h {
		hs[k] = &v1.HeaderValues{Values: v}
	}
	s := &stream{ctx: context.Background(), in: []*v1.ForwardRequest{{Frame: &v1.ForwardRequest_Start{Start: &v1.ForwardRequestStart{AccountId: 17, Method: "POST", Url: "https://chatgpt.com/backend-api/codex/responses", Headers: hs}}}, {Frame: &v1.ForwardRequest_BodyChunk{BodyChunk: body}}, {Frame: &v1.ForwardRequest_BodyEnd{BodyEnd: true}}}}
	if e := p.Forward(s); e != nil {
		t.Fatal(e)
	}
	if len(s.out) < 3 || s.out[0].GetStart().StatusCode != 200 || s.out[len(s.out)-1].GetEnd() == nil {
		t.Fatal("invalid response frame sequence")
	}
}

func protectedStream(ctx context.Context, accountID int64, h http.Header) *stream {
	hs := map[string]*v1.HeaderValues{}
	for k, values := range h {
		hs[k] = &v1.HeaderValues{Values: values}
	}
	body := []byte(`{"model":"gpt-6-astra","input":"test","stream":true}`)
	return &stream{ctx: ctx, in: []*v1.ForwardRequest{
		{Frame: &v1.ForwardRequest_Start{Start: &v1.ForwardRequestStart{AccountId: accountID, Method: "POST", Url: "https://chatgpt.com/backend-api/codex/responses", Headers: hs}}},
		{Frame: &v1.ForwardRequest_BodyChunk{BodyChunk: body}},
		{Frame: &v1.ForwardRequest_BodyEnd{BodyEnd: true}},
	}}
}

func TestBusiness429RetainsCurrentTicket(t *testing.T) {
	h := headers()
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	ticket := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix())
	entry := &Entry{ticket: ticket}
	var calls atomic.Int32
	p.config.Accounts = []int64{17}
	p.entries[key(17, ticket.Model, ticket.CredentialHash)] = entry
	p.businessClientFactory = func(string) (*http.Client, error) {
		return &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) {
			calls.Add(1)
			return &http.Response{StatusCode: 429, Status: "429 Too Many Requests", Header: make(http.Header), Body: io.NopCloser(bytes.NewReader(nil))}, nil
		})}, nil
	}
	if err := p.Forward(protectedStream(context.Background(), 17, h)); err != nil {
		t.Fatal(err)
	}
	second := protectedStream(context.Background(), 17, h)
	if err := p.Forward(second); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 || second.out[0].GetStart().StatusCode != 429 {
		t.Fatal("business backoff forwarded another request")
	}
	entry.mu.Lock()
	defer entry.mu.Unlock()
	if entry.ticket.Value != ticket.Value {
		t.Fatal("429 discarded a valid ticket")
	}
	if !entry.cooldown.After(time.Now()) {
		t.Fatal("429 did not start harvest cooldown")
	}
}

func TestStaleAuthResponseCannotDeleteNewTicket(t *testing.T) {
	h := headers()
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	old := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix())
	newer := makeTicket(17, "gpt-6-astra", identity(h), old.Issued+1)
	entry := &Entry{ticket: old}
	p.config.Accounts = []int64{17}
	p.entries[key(17, old.Model, old.CredentialHash)] = entry
	p.businessClientFactory = func(string) (*http.Client, error) {
		return &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) {
			entry.mu.Lock()
			entry.ticket = newer
			entry.mu.Unlock()
			return &http.Response{StatusCode: 401, Status: "401 Unauthorized", Header: make(http.Header), Body: io.NopCloser(bytes.NewReader(nil))}, nil
		})}, nil
	}
	if err := p.Forward(protectedStream(context.Background(), 17, h)); err != nil {
		t.Fatal(err)
	}
	entry.mu.Lock()
	defer entry.mu.Unlock()
	if entry.ticket.Value != newer.Value || entry.blocked {
		t.Fatal("stale response invalidated a newer ticket")
	}
}

func TestBusinessConcurrencyLimitedPerAccount(t *testing.T) {
	h := headers()
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	ticket := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix())
	p.config.Accounts = []int64{17}
	p.entries[key(17, ticket.Model, ticket.CredentialHash)] = &Entry{ticket: ticket}
	entered := make(chan struct{}, 4)
	release := make(chan struct{}, 4)
	var active atomic.Int32
	var maximum atomic.Int32
	p.businessClientFactory = func(string) (*http.Client, error) {
		return &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) {
			n := active.Add(1)
			for old := maximum.Load(); n > old && !maximum.CompareAndSwap(old, n); old = maximum.Load() {
			}
			entered <- struct{}{}
			<-release
			active.Add(-1)
			return &http.Response{StatusCode: 200, Status: "200 OK", Header: make(http.Header), Body: io.NopCloser(bytes.NewReader(nil))}, nil
		})}, nil
	}
	var wg sync.WaitGroup
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := p.Forward(protectedStream(context.Background(), 17, h)); err != nil {
				t.Error(err)
			}
		}()
	}
	<-entered
	<-entered
	select {
	case <-entered:
		t.Fatal("more than two business requests entered concurrently")
	case <-time.After(100 * time.Millisecond):
	}
	release <- struct{}{}
	release <- struct{}{}
	<-entered
	<-entered
	release <- struct{}{}
	release <- struct{}{}
	wg.Wait()
	if maximum.Load() != businessConcurrencyPerAccount {
		t.Fatalf("maximum concurrency=%d", maximum.Load())
	}
}
func TestForwardLongConversationsConcurrently(t *testing.T) {
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			t.Run(fmt.Sprint(time.Now().UnixNano()), func(t *testing.T) {
				h := headers()
				p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
				ticket := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix())
				p.businessClientFactory = func(string) (*http.Client, error) { return p.client, nil }
				p.config.Accounts = []int64{17}
				p.entries[key(17, ticket.Model, ticket.CredentialHash)] = &Entry{ticket: ticket}
				body := []byte(`{"model":"gpt-6-astra","input":[{"role":"user","content":"hello"}],"stream":true}`)
				body = append(body, bytes.Repeat([]byte(" "), 5<<20)...)
				p.client = &http.Client{Transport: roundtrip(func(r *http.Request) (*http.Response, error) {
					b, _ := io.ReadAll(r.Body)
					if !bytes.Equal(b, body) {
						t.Error("business body changed")
					}
					if r.Header.Get(stateHeader) != ticket.Value {
						t.Error("not injected")
					}
					return &http.Response{StatusCode: 200, Status: "200 OK", Proto: "HTTP/2.0", ProtoMajor: 2, Header: make(http.Header), Body: io.NopCloser(bytes.NewBufferString("data: {\"type\":\"response.completed\",\"response\":{\"model\":\"gpt-6-astra\"}}\n\n"))}, nil
				})}
				hs := map[string]*v1.HeaderValues{}
				for k, v := range h {
					hs[k] = &v1.HeaderValues{Values: v}
				}
				s := &stream{ctx: context.Background(), in: []*v1.ForwardRequest{{Frame: &v1.ForwardRequest_Start{Start: &v1.ForwardRequestStart{AccountId: 17, Method: "POST", Url: "https://chatgpt.com/backend-api/codex/responses", Headers: hs}}}, {Frame: &v1.ForwardRequest_BodyChunk{BodyChunk: body}}, {Frame: &v1.ForwardRequest_BodyEnd{BodyEnd: true}}}}
				if e := p.Forward(s); e != nil {
					t.Fatal(e)
				}
				if len(s.out) < 3 || s.out[0].GetStart().StatusCode != 200 || s.out[len(s.out)-1].GetEnd() == nil {
					t.Fatal("invalid response frame sequence")
				}
			})
		}()
	}
	wg.Wait()
}
func TestExpiryAndWrongShape(t *testing.T) {
	now := time.Now()
	v := makeTicket(17, "gpt-6-astra", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", now.Unix()-3600)
	if validTicket(v, now) {
		t.Fatal("expired accepted")
	}
	v.Issued = now.Unix()
	v.Value = v.Value[:322]
	if validTicket(v, now) {
		t.Fatal("322 accepted")
	}
}

func TestMissingTicketForwardsNormally(t *testing.T) {
	for _, model := range []string{"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"} {
		t.Run(model, func(t *testing.T) {
			p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
			p.businessClientFactory = func(string) (*http.Client, error) { return p.client, nil }
			p.config.Accounts = []int64{17}
			p.config.Suspended = []int64{17}
			var calls int
			p.client = &http.Client{Transport: roundtrip(func(r *http.Request) (*http.Response, error) {
				calls++
				if r.Header.Get(stateHeader) != "" {
					t.Fatal("missing ticket forwarded stale header")
				}
				return &http.Response{StatusCode: 200, Status: "200 OK", Header: make(http.Header), Body: io.NopCloser(bytes.NewBufferString("ok"))}, nil
			})}
			body := []byte(fmt.Sprintf(`{"model":%q}`, model))
			s := &stream{ctx: context.Background(), in: []*v1.ForwardRequest{{Frame: &v1.ForwardRequest_Start{Start: &v1.ForwardRequestStart{AccountId: 17, Method: "POST", Url: "https://chatgpt.com/backend-api/codex/responses"}}}, {Frame: &v1.ForwardRequest_BodyChunk{BodyChunk: body}}, {Frame: &v1.ForwardRequest_BodyEnd{BodyEnd: true}}}}
			if err := p.Forward(s); err != nil {
				t.Fatal(err)
			}
			if calls != 1 || s.out[0].GetStart() == nil || s.out[0].GetStart().StatusCode != 200 {
				t.Fatal("availability fallback failed")
			}
		})
	}
}

func TestVerifiedCandidateImport(t *testing.T) {
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	h := headers()
	v := makeTicket(29, "gpt-6-astra", identity(h), time.Now().Unix())
	p.config.Accounts = []int64{29}
	p.client = &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) { t.Fatal("import should not harvest"); return nil, nil })}
	k := key(29, v.Model, v.CredentialHash)
	dir := filepath.Join(filepath.Dir(p.store), "incoming")
	os.Mkdir(dir, 0700)
	b, _ := json.Marshal(v)
	f := filepath.Join(dir, digest(k)+".json")
	os.WriteFile(f, b, 0600)
	got, err := p.ticket(context.Background(), 29, v.Model, h)
	if err != nil || got.Value != v.Value {
		t.Fatal("import failed", err)
	}
	if _, err = os.Stat(f); !os.IsNotExist(err) {
		t.Fatal("candidate not consumed")
	}
	b, err = os.ReadFile(p.store)
	if err != nil {
		t.Fatal(err)
	}
	var stored []Ticket
	json.Unmarshal(b, &stored)
	if len(stored) != 1 || stored[0].Value != v.Value {
		t.Fatal("not persisted")
	}
}

func TestVerified292CandidateImport(t *testing.T) {
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	h := headers()
	v := makeTicket(29, "gpt-6-astra", identity(h), time.Now().Unix())
	raw := make([]byte, 217)
	raw[0] = 128
	binary.BigEndian.PutUint64(raw[1:9], uint64(v.Issued))
	v.Value = base64.URLEncoding.EncodeToString(raw)
	p.config.Accounts = []int64{29}
	p.client = &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) { t.Fatal("import should not harvest"); return nil, nil })}
	k := key(29, v.Model, v.CredentialHash)
	dir := filepath.Join(filepath.Dir(p.store), "incoming")
	os.Mkdir(dir, 0700)
	b, _ := json.Marshal(v)
	f := filepath.Join(dir, digest(k)+".json")
	os.WriteFile(f, b, 0600)
	got, err := p.ticket(context.Background(), 29, v.Model, h)
	if err != nil || got.Value != v.Value {
		t.Fatal("import failed", err)
	}
	if _, err = os.Stat(f); !os.IsNotExist(err) {
		t.Fatal("candidate not consumed")
	}
	b, err = os.ReadFile(p.store)
	if err != nil {
		t.Fatal(err)
	}
	var stored []Ticket
	json.Unmarshal(b, &stored)
	if len(stored) != 1 || stored[0].Value != v.Value {
		t.Fatal("not persisted")
	}
}

func TestAccountSlotsIsolationAndCancellation(t *testing.T) {
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	r1, _ := p.acquireBusiness(context.Background(), 17)
	defer r1()
	r2, _ := p.acquireBusiness(context.Background(), 17)
	defer r2()
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if release, err := p.acquireBusiness(ctx, 17); err == nil {
		release()
		t.Fatal("canceled waiter acquired full slot")
	}
	other, err := p.acquireBusiness(context.Background(), 18)
	if err != nil {
		t.Fatal(err)
	}
	other()
}

func harvestHeaders(state string) http.Header {
	return http.Header{
		stateHeader: []string{state},
		"Set-Cookie": []string{
			"session=abc; Path=/; HttpOnly; Secure",
			"other=xyz",
		},
	}
}

func TestHarvestCapturesCookiesAndReplaysOnForward(t *testing.T) {
	h := headers()
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	state := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix()).Value
	p.config.Accounts = []int64{17}
	p.client = &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) {
		return &http.Response{StatusCode: 200, Header: harvestHeaders(state), Body: io.NopCloser(bytes.NewBufferString("data: {\"type\":\"response.completed\",\"response\":{\"model\":\"gpt-6-astra\"}}\n\n"))}, nil
	})}
	var cookie string
	var injected string
	p.businessClientFactory = func(string) (*http.Client, error) {
		return &http.Client{Transport: roundtrip(func(r *http.Request) (*http.Response, error) {
			cookie = r.Header.Get("Cookie")
			injected = r.Header.Get(stateHeader)
			return &http.Response{StatusCode: 200, Status: "200 OK", Header: make(http.Header), Body: io.NopCloser(bytes.NewBufferString("ok"))}, nil
		})}, nil
	}
	s := protectedStream(context.Background(), 17, h)
	if err := p.Forward(s); err != nil {
		t.Fatal(err)
	}
	if injected != state {
		t.Fatal("state not injected after harvest")
	}
	if cookie != "session=abc; other=xyz" {
		t.Fatalf("cookie replay=%q", cookie)
	}
	b, err := os.ReadFile(p.store)
	if err != nil {
		t.Fatal(err)
	}
	var stored []Ticket
	json.Unmarshal(b, &stored)
	if len(stored) != 1 || len(stored[0].Cookies) != 2 || stored[0].Cookies[0] != "session=abc" {
		t.Fatalf("harvested cookies not persisted: %v", stored)
	}
}

func TestBundleExpiryBoundaries(t *testing.T) {
	now := time.Now()
	if !validTicket(makeTicket(17, "gpt-6-astra", strings.Repeat("a", 64), now.Unix()-239), now) {
		t.Fatal("239s old bundle rejected")
	}
	if validTicket(makeTicket(17, "gpt-6-astra", strings.Repeat("a", 64), now.Unix()-241), now) {
		t.Fatal("241s old bundle accepted")
	}
}

func TestExpiredBundleIsReharvested(t *testing.T) {
	h := headers()
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	// Valid per the 240s upstream window but past the reuse margin: the plugin
	// must harvest a fresh bundle instead of replaying a dying one.
	stale := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix()-211)
	p.config.Accounts = []int64{17}
	p.entries[key(17, stale.Model, stale.CredentialHash)] = &Entry{ticket: stale}
	var harvests atomic.Int32
	state := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix()).Value
	p.client = &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) {
		harvests.Add(1)
		return &http.Response{StatusCode: 200, Header: harvestHeaders(state), Body: io.NopCloser(bytes.NewBufferString("data: {\"type\":\"response.completed\",\"response\":{\"model\":\"gpt-6-astra\"}}\n\n"))}, nil
	})}
	got, e := p.ticket(withProxy(context.Background(), "http://127.0.0.1:18301"), 17, "gpt-6-astra", h)
	if e != nil || got.Value != state || len(got.Cookies) != 2 {
		t.Fatal("fresh bundle not harvested", e)
	}
	if harvests.Load() != 1 {
		t.Fatal("stale bundle reused past margin")
	}
}

func TestImportedCandidateCarriesCookies(t *testing.T) {
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	h := headers()
	v := makeTicket(29, "gpt-6-astra", identity(h), time.Now().Unix())
	v.Cookies = []string{"session=imported"}
	p.config.Accounts = []int64{29}
	p.client = &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) { t.Fatal("import should not harvest"); return nil, nil })}
	k := key(29, v.Model, v.CredentialHash)
	dir := filepath.Join(filepath.Dir(p.store), "incoming")
	os.Mkdir(dir, 0700)
	b, _ := json.Marshal(v)
	os.WriteFile(filepath.Join(dir, digest(k)+".json"), b, 0600)
	got, err := p.ticket(context.Background(), 29, v.Model, h)
	if err != nil || got.Value != v.Value || len(got.Cookies) != 1 || got.Cookies[0] != "session=imported" {
		t.Fatal("imported cookies lost", err)
	}
}
