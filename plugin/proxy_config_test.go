package main

import (
	"encoding/json"
	"testing"
)

func TestProxyConfigGeneratorEndpointsAndScope(t *testing.T) {
	for _, endpoint := range []string{"", "http://provider.example/gen?key=secret", "https://provider.example/api/get-ip?key=secret"} {
		raw, _ := json.Marshal(map[string]any{"proxy_url": "socks5h://user:password@host:1080", "harvest_proxy_api": endpoint, "accounts": []int64{17, 19}, "suspended": []int64{19}})
		cfg, err := parseConfig(raw)
		if err != nil || cfg.HarvestProxyAPI != endpoint || len(cfg.Accounts) != 2 || len(cfg.Suspended) != 1 {
			t.Fatalf("valid config rejected: %v", err)
		}
	}
	for _, endpoint := range []string{"file:///tmp/key", "ftp://host", "https://user:pass@host/api", "https://host/api#secret", "https:///api"} {
		raw, _ := json.Marshal(map[string]any{"proxy_url": "http://host:80", "harvest_proxy_api": endpoint})
		if _, err := parseConfig(raw); err == nil {
			t.Fatal("invalid generator accepted")
		}
	}
}
