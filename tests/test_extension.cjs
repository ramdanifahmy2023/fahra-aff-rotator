const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const backgroundSource = readFileSync('extension/background.js', 'utf8');

async function bridge({ configured = true, tabCount = 1, wrongAccount = false, oldContent = false } = {}) {
  const calls = [];
  const stored = { accountId: 'a'.repeat(32), ...(configured ? { accountName: 'Test profile' } : {}) };
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  let runCount = 0;
  let statusListener;
  const context = vm.createContext({
    crypto: require('node:crypto').webcrypto,
    setTimeout: () => 1, setInterval: () => 2, clearInterval: () => {},
    chrome: {
      storage: { local: { get: async () => stored, set: async value => Object.assign(stored, value) } },
      tabs: {
        query: async () => Array.from({length: tabCount}, (_, id) => ({id, url: 'https://affiliate.shopee.co.id/offer/custom_link'})),
        sendMessage: async (_id, message) => {
          if (message.type === 'ping') return { ready: true, protocol_version: oldContent ? 1 : 2 };
          runCount++;
          await gate;
          return { results: [{shortLink: 'https://s.shopee.co.id/Test1'}] };
        },
      },
      runtime: { onMessage: {addListener: listener => {statusListener = listener;}}, onInstalled: {addListener() {}}, openOptionsPage() {} },
      action: {onClicked: {addListener() {}}}, alarms: {create() {}, onAlarm: {addListener() {}}},
    },
    fetch: async (url, options) => {
      const body = JSON.parse(options.body);
      calls.push({url, body});
      return { ok: true, json: async () => url.endsWith('/next')
        ? {job: {id: 'test-job', account_id: (wrongAccount ? 'b' : 'a').repeat(32), links: ['https://shopee.co.id/test']}, lease_token: 'test-only'}
        : {ok: true} };
    },
  });
  vm.runInContext(backgroundSource, context);
  await new Promise(resolve => setImmediate(resolve));
  return { calls, stored, context, release, runs: () => runCount,
    status: () => {let value; statusListener({type:'bridge_status'}, {}, result => {value = result;}); return value;} };
}

test('overlapping polling cannot execute the same job twice', async () => {
  const instance = await bridge();
  await Promise.all([vm.runInContext('tick()', instance.context), vm.runInContext('tick()', instance.context)]);
  assert.equal(instance.runs(), 1);
  assert.equal(instance.calls.filter(call => call.url.endsWith('/next')).length, 1);
  instance.release();
  await new Promise(resolve => setImmediate(resolve));
  const result = instance.calls.find(call => call.url.endsWith('/result'));
  assert.equal(result.body.account_id, instance.stored.accountId);
  assert.equal(result.body.protocol_version, 2);
  assert.equal(result.body.job_id, 'test-job');
});

test('unconfigured profiles never claim jobs', async () => {
  const instance = await bridge({configured:false});
  assert.equal(instance.calls.length, 0);
  assert.match(instance.status().error, /nama akun/);
});

test('ambiguous tabs report not ready and never claim', async () => {
  const instance = await bridge({tabCount:2});
  assert.equal(instance.calls.length, 1);
  assert.equal(instance.calls[0].body.ready, false);
  assert.match(instance.status().error, /satu tab/);
});

test('new background refuses content scripts from before the upgrade', async () => {
  const instance = await bridge({oldContent:true});
  assert.equal(instance.calls.length, 1);
  assert.equal(instance.calls[0].body.ready, false);
});

test('foreign account jobs never reach the Shopee tab', async () => {
  const instance = await bridge({wrongAccount:true});
  assert.equal(instance.runs(), 0);
  assert.match(instance.status().error, /tidak cocok/);
});

test('content recovery only returns the successful result for the same job', async () => {
  let listener;
  const context = vm.createContext({
    chrome: { runtime: {onMessage: {addListener: handler => {listener = handler;}}} },
    location: {pathname:'/offer/custom_link'},
  });
  vm.runInContext(readFileSync('extension/content.js', 'utf8'), context);
  vm.runInContext('runJob = async () => ({results:[{shortLink:"https://s.shopee.co.id/Fresh1"}]})', context);
  let response;
  listener({type:'collect_result', job_id:'new-job'}, {}, result => {response = result;});
  assert.equal(response, null);
  await new Promise(resolve => listener({type:'run_job', job:{id:'new-job'}}, {}, resolve));
  await new Promise(resolve => setImmediate(resolve));
  listener({type:'collect_result', job_id:'other-job'}, {}, result => {response = result;});
  assert.equal(response, null);
  listener({type:'collect_result', job_id:'new-job'}, {}, result => {response = result;});
  assert.equal(response.results[0].shortLink, 'https://s.shopee.co.id/Fresh1');
  vm.runInContext('runJob = async () => ({error:"needs manual check"})', context);
  await new Promise(resolve => listener({type:'run_job', job:{id:'failed-job'}}, {}, resolve));
  await new Promise(resolve => setImmediate(resolve));
  listener({type:'collect_result', job_id:'failed-job'}, {}, result => {response = result;});
  assert.equal(response, null);
});
