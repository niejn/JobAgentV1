import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';
import { test } from 'node:test';

const source = readFileSync(new URL('./src/App.tsx', import.meta.url), 'utf8');
const handlers = source.slice(source.indexOf('  function acceptExtraction('), source.indexOf('  async function submit()'));
const js = ts.transpileModule(handlers, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;

test('text and corrections render the backend summary unchanged', async () => {
  for (const step of ['input', 'questions', 'confirm']) {
    let rendered, submitted;
    const previous = { company: '旧公司', role: '', description: '原始JD' };
    const summary = { company: '狼腾知光', role: '算法与应用开发工程师', department: '', location: '上海临港', description: '原始JD' };
    const context = vm.createContext({
      input: '企业名称狼腾知光 岗位名称算法与应用开发工程师', busy: false, ocrBusy: false, step, draft: previous, AbortSignal,
      api: async (url, options) => { assert.equal(url, '/api/journey-drafts/extract'); submitted = JSON.parse(options.body); return { draft: summary, missing_fields: [], message: '请确认' }; },
      setDraft: value => { rendered = value; }, setMessages() {}, setInput() {}, setStep() {},
      setMatchOpinion() {}, evaluateMatch() {}, setBusy() {}, setError: value => { assert.equal(value, ''); },
    });
    vm.runInContext(js, context);
    await context.handleReply({ preventDefault() {} });
    assert.equal(rendered, summary);
    assert.deepEqual(submitted.current, step === 'input' ? null : previous);
  }
});

test('failed extraction preserves the draft and reports an error', async () => {
  let error;
  const context = vm.createContext({
    input: 'JD', busy: false, ocrBusy: false, step: 'input', draft: {}, AbortSignal,
    api: async () => { throw new Error('offline'); },
    setBusy() {}, setError: value => { error = value; },
    setDraft: () => assert.fail('must not overwrite draft'),
  });
  vm.runInContext(js, context);
  await context.handleReply({ preventDefault() {} });
  assert.ok(error);
});
