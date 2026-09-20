package main

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"path/filepath"
	"testing"

	v1 "github.com/Wei-Shaw/sub2api/pkg/pluginapi/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

func TestV1LifecycleRPCCompatibility(t *testing.T) {
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	v1.RegisterTransportPluginServer(server, &v1Adapter{plugin: NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))})
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	connection, err := grpc.NewClient("passthrough:///plugin", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	client := v1.NewTransportPluginClient(connection)
	ctx := context.Background()
	info, err := client.GetInfo(ctx, &v1.GetInfoRequest{})
	if err != nil || info.GetProtocolVersion() != 1 || info.GetPluginVersion() != compatibilityVersion {
		t.Fatalf("incompatible runtime metadata: %v %v", info, err)
	}
	invalid, err := client.ValidateConfig(ctx, &v1.ValidateConfigRequest{ConfigJson: []byte(`{}`)})
	if err != nil || invalid.GetValid() {
		t.Fatalf("invalid configuration accepted: %v %v", invalid, err)
	}
	configuration := []byte(`{"proxy_url":"http://127.0.0.1:18300","accounts":[17]}`)
	valid, err := client.ValidateConfig(ctx, &v1.ValidateConfigRequest{ConfigJson: configuration})
	if err != nil || !valid.GetValid() {
		t.Fatalf("configuration rejected: %v %v", valid, err)
	}
	applied, err := client.ApplyConfig(ctx, &v1.ApplyConfigRequest{ConfigJson: valid.GetNormalizedConfigJson()})
	if err != nil || !applied.GetApplied() {
		t.Fatalf("configuration not applied: %v %v", applied, err)
	}
	tested, err := client.TestConfig(ctx, &v1.TestConfigRequest{ConfigJson: configuration})
	if err != nil || !tested.GetSuccess() {
		t.Fatalf("configuration test failed: %v %v", tested, err)
	}
	health, err := client.Health(ctx, &v1.HealthRequest{})
	if err != nil || !health.GetHealthy() {
		t.Fatalf("runtime unhealthy: %v %v", health, err)
	}
}

func TestV1UnselectedRequestsPreserveBodyHeadersAndProxy(t *testing.T) {
	for _, upstream := range []string{"https://chatgpt.com/backend-api/codex/responses", "https://example.invalid/custom"} {
		t.Run(upstream, func(t *testing.T) {
			p := NewPlugin(filepath.Join(t.TempDir(), "tickets.json"))
			p.config.Accounts = []int64{17}
			body := []byte("opaque multipart body\x00\xff")
			p.businessClientFactory = func(proxy string) (*http.Client, error) {
				if proxy != "socks5://127.0.0.1:1234" {
					t.Fatalf("business proxy changed: %q", proxy)
				}
				return &http.Client{Transport: roundtrip(func(request *http.Request) (*http.Response, error) {
					actual, _ := io.ReadAll(request.Body)
					if !bytes.Equal(actual, body) || request.Header.Get(stateHeader) != "client-state" {
						t.Fatal("unselected request was modified")
					}
					return &http.Response{StatusCode: 200, Header: make(http.Header), Body: io.NopCloser(bytes.NewBufferString("ok"))}, nil
				})}, nil
			}
			s := &stream{ctx: context.Background(), in: []*v1.ForwardRequest{
				{Frame: &v1.ForwardRequest_Start{Start: &v1.ForwardRequestStart{AccountId: 18, Method: "POST", Url: upstream,
					ProxyUrl: "socks5://127.0.0.1:1234", Headers: map[string]*v1.HeaderValues{stateHeader: {Values: []string{"client-state"}}}}}},
				{Frame: &v1.ForwardRequest_BodyChunk{BodyChunk: body}},
				{Frame: &v1.ForwardRequest_BodyEnd{BodyEnd: true}},
			}}
			if err := p.Forward(s); err != nil {
				t.Fatal(err)
			}
			if len(s.out) < 3 || s.out[0].GetStart().GetStatusCode() != 200 || s.out[len(s.out)-1].GetEnd() == nil {
				t.Fatal("unselected request was blocked")
			}
		})
	}
}
