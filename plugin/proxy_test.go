package main

import (
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

func TestBusinessAuthenticatedProxy(t *testing.T) {
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		want := "Basic " + base64.StdEncoding.EncodeToString([]byte("user:p@ss:word"))
		if r.Header.Get("Proxy-Authorization") != want {
			t.Error("proxy authentication missing")
			w.WriteHeader(407)
			return
		}
		if r.URL.Host != "example.invalid" {
			t.Error("unexpected destination")
		}
		w.Write([]byte("ok"))
	}))
	defer proxy.Close()
	u, _ := url.Parse(proxy.URL)
	u.User = url.UserPassword("user", "p@ss:word")
	p := NewPlugin("")
	c, err := p.businessClient(u.String())
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseIdleConnections()
	r, err := c.Get("http://example.invalid/test")
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	b, _ := io.ReadAll(r.Body)
	if r.StatusCode != 200 || string(b) != "ok" {
		t.Fatal("proxy forwarding failed")
	}
}

func TestBusinessProxySchemesAndDirectIsolation(t *testing.T) {
	p := NewPlugin("")
	for _, scheme := range []string{"http", "https", "socks5", "socks5h"} {
		raw := scheme + "://user:pass@127.0.0.1:12345"
		c, err := p.businessClient(raw)
		if err != nil {
			t.Fatal(err)
		}
		tr := c.Transport.(*http.Transport)
		u, err := tr.Proxy(&http.Request{})
		if err != nil || u.String() != raw {
			t.Fatal("proxy changed")
		}
		c.CloseIdleConnections()
	}
	for _, raw := range []string{"ftp://host:21", "http://", "http://host/path", "http://host?query=1"} {
		if _, err := p.businessClient(raw); err == nil {
			t.Fatal("invalid proxy accepted")
		}
	}
	t.Setenv("HTTPS_PROXY", "http://127.0.0.1:12345")
	c, err := p.businessClient("")
	if err != nil {
		t.Fatal(err)
	}
	defer c.CloseIdleConnections()
	if c.Transport.(*http.Transport).Proxy != nil {
		t.Fatal("direct path inherited ambient proxy")
	}
}
