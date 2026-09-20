package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	v1 "github.com/Wei-Shaw/sub2api/pkg/pluginapi/v1"
)

func TestSolVerifiedCandidateIsImportedAndInjectedWithoutAstraReuse(t *testing.T) {
	p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
	p.config.Accounts = []int64{17}
	h := headers()
	astra := makeTicket(17, "gpt-6-astra", identity(h), time.Now().Unix()-10)
	sol := makeTicket(17, "gpt-5.6-sol", identity(h), time.Now().Unix())
	p.entries[key(17, astra.Model, astra.CredentialHash)] = &Entry{ticket: astra}
	if err := p.persist(key(17, astra.Model, astra.CredentialHash), astra); err != nil {
		t.Fatal(err)
	}
	incoming := filepath.Join(filepath.Dir(p.store), "incoming")
	if err := os.Mkdir(incoming, 0700); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(sol)
	candidate := filepath.Join(incoming, digest(key(17, sol.Model, sol.CredentialHash))+".json")
	if err := os.WriteFile(candidate, raw, 0600); err != nil {
		t.Fatal(err)
	}
	p.client = &http.Client{Transport: roundtrip(func(*http.Request) (*http.Response, error) {
		t.Fatal("verified Sol candidate should be used without harvesting")
		return nil, nil
	})}
	calls := 0
	p.businessClientFactory = func(proxy string) (*http.Client, error) {
		if proxy != "socks5://127.0.0.1:1000" {
			t.Fatal("business proxy not preserved")
		}
		return &http.Client{Transport: roundtrip(func(request *http.Request) (*http.Response, error) {
			calls++
			if request.Header.Get(stateHeader) != sol.Value || request.Header.Get(stateHeader) == astra.Value {
				t.Fatal("Sol did not use its own ticket")
			}
			body, _ := io.ReadAll(request.Body)
			if !bytes.Equal(body, []byte(`{"model":"gpt-5.6-sol"}`)) {
				t.Fatal("model request changed")
			}
			return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(bytes.NewBufferString("ok"))}, nil
		})}, nil
	}
	hs := map[string]*v1.HeaderValues{}
	for name, values := range h {
		hs[name] = &v1.HeaderValues{Values: values}
	}
	s := &stream{ctx: context.Background(), in: []*v1.ForwardRequest{
		{Frame: &v1.ForwardRequest_Start{Start: &v1.ForwardRequestStart{AccountId: 17, Method: "POST", Url: "https://chatgpt.com/backend-api/codex/responses", Headers: hs, ProxyUrl: "socks5://127.0.0.1:1000"}}},
		{Frame: &v1.ForwardRequest_BodyChunk{BodyChunk: []byte(`{"model":"gpt-5.6-sol"}`)}},
		{Frame: &v1.ForwardRequest_BodyEnd{BodyEnd: true}},
	}}
	if err := p.Forward(s); err != nil {
		t.Fatal(err)
	}
	if calls != 1 || s.out[0].GetStart().GetStatusCode() != 200 {
		t.Fatal("Sol request did not forward")
	}
	if _, err := os.Stat(candidate); !os.IsNotExist(err) {
		t.Fatal("candidate was not consumed")
	}
	stored, err := os.ReadFile(p.store)
	if err != nil {
		t.Fatal(err)
	}
	var tickets []Ticket
	if json.Unmarshal(stored, &tickets) != nil || len(tickets) != 2 {
		t.Fatal("ticket store lost a model")
	}
}
