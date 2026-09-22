const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '../plugin/ui');
const html = fs.readFileSync(path.join(root,'index.html'),'utf8');
const code = fs.readFileSync(path.join(root,'app.js'),'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

async function mount() {
  const dom = new JSDOM(html,{url:'https://example.invalid/plugin/index.html#bridge_token=test-token',referrer:'https://example.invalid/admin/plugins',runScripts:'outside-only'});
  const w = dom.window;
  let config = {proxy_url:'socks5h://user:SECRET@proxy:1080',accounts:[17],suspended:[18]};
  const calls = [];
  const host = {postMessage(message, target) {
    calls.push(message);
    assert.equal(target,'https://example.invalid');
    if (message.type === 'sub2api.plugin.ready') return;
    if (message.type === 'config.save') config = message.config;
    queueMicrotask(() => w.dispatchEvent(new w.MessageEvent('message',{source:host,origin:'https://example.invalid',data:{source:'sub2api-plugin-host',bridge_token:'test-token',request_id:message.request_id,ok:true,config}})));
  }};
  Object.defineProperty(w,'parent',{value:host});
  w.eval(code); await tick();
  return {dom,calls,config:()=>config,setConfig:value=>{config=value;}};
}

test('proxy configuration uses host bridge and preserves current enrollment', async () => {
  const {dom,calls,config,setConfig} = await mount();
  try {
    const d = dom.window.document;
    assert.equal(d.getElementById('proxy').type,'password');
    assert.equal(d.getElementById('proxy').value,'socks5h://user:SECRET@proxy:1080');
    setConfig({...config(),accounts:[17,19],suspended:[18,20]});
    d.getElementById('proxy').value='http://new-user:new-password@new:80';
    d.getElementById('generator').value='https://provider.example/api?key=private';
    d.getElementById('save').click(); await tick();
    assert.deepEqual(Array.from(config().accounts),[17,19]);
    assert.deepEqual(Array.from(config().suspended),[18,20]);
    assert.equal(config().harvest_proxy_api,'https://provider.example/api?key=private');
    assert.equal(calls.filter(x=>x.type==='config.save').length,1);
    assert.match(d.getElementById('status').textContent,/已保存/);
    assert.equal(dom.window.localStorage.length,0);
  } finally {dom.window.close();}
});

test('bad proxy input does not write and generator can be cleared explicitly', async () => {
  const {dom,calls,config} = await mount();
  try {
    const d=dom.window.document;
    d.getElementById('proxy').value='ftp://host:21';d.getElementById('save').click();await tick();
    assert.equal(calls.filter(x=>x.type==='config.save').length,0);
    d.getElementById('proxy').value='socks5h://host:1080';d.getElementById('generator').value='';
    d.getElementById('save').click();await tick();
    assert.equal(config().harvest_proxy_api,'');
  } finally {dom.window.close();}
});

test('untrusted bridge messages cannot supply configuration', async () => {
  const dom=new JSDOM(html,{url:'https://example.invalid/plugin/#bridge_token=t',referrer:'https://example.invalid/admin/plugins',runScripts:'outside-only'});
  const w=dom.window,calls=[];const host={postMessage:m=>calls.push(m)};
  Object.defineProperty(w,'parent',{value:host});w.eval(code);
  const request=calls.find(m=>m.type==='config.load');
  const response={source:'sub2api-plugin-host',bridge_token:'t',request_id:request.request_id,ok:true,config:{proxy_url:'http://injected:80'}};
  try {
    for(const fields of [{source:{},origin:'https://example.invalid'}, {source:host,origin:'https://evil.invalid'}])
      w.dispatchEvent(new w.MessageEvent('message',{...fields,data:response}));
    w.dispatchEvent(new w.MessageEvent('message',{source:host,origin:'https://example.invalid',data:{...response,bridge_token:'wrong'}}));
    await tick();assert.equal(w.document.getElementById('proxy').value,'');
  } finally {dom.window.close();}
});
