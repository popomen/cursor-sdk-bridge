/**
 * git 凭证来源的解析测试。
 *
 * 这条路径有个反直觉的地方：`git ls-remote` 对签发仓库**必然**以非零码退出
 * （403，那个仓库本来就不给读），而我们要的 JWT 恰恰在那次失败的输出里。
 * 所以「命令失败」不等于「取不到凭证」，两者必须分开判断。
 */
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { extractCodebaseJwt, tokenExpiry, parseTraeToken } from "../src/upstream/credentials.js";

/** 线上真实输出（JWT 换成结构相同的假值） */
const FAKE_JWT =
  "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9.eyJhdWQiOiJDb2RlIiwiZXhwIjoxNzg3NDI0MjQ0LCJpYXQiOjE3ODYyMTQ2NDQsInN1YiI6InRlc3QifQ.c2ln";

const REAL_OUTPUT = `X-Code-API-Endpoint https://code.byted.org/api/v2/
X-Code-JWT ${FAKE_JWT}
---------------------------------------------------------------
 Exit  | 403
---------------------------------------------------------------
 LogID | 021786214644201fdbddc0100ffcd02ffffffff000008dff273d9
---------------------------------------------------------------
fatal: Could not read from remote repository.`;

test("从 git 的 403 输出里抠出 JWT", () => {
  assert.equal(extractCodebaseJwt(REAL_OUTPUT), FAKE_JWT);
});

test("CRLF 换行同样能解析", () => {
  assert.equal(extractCodebaseJwt(REAL_OUTPUT.replace(/\n/g, "\r\n")), FAKE_JWT);
});

test("JWT 出现在首行也能解析", () => {
  assert.equal(extractCodebaseJwt(`X-Code-JWT ${FAKE_JWT}\nfatal: whatever`), FAKE_JWT);
});

test("冒号分隔的写法也能解析", () => {
  assert.equal(extractCodebaseJwt(`X-Code-JWT: ${FAKE_JWT}`), FAKE_JWT);
});

test("没有 JWT 时返回 null，不返回空串", () => {
  // SSH 身份不可用时的典型输出 —— 必须能和「拿到了」区分开
  const denied = `Permission denied (publickey).\nfatal: Could not read from remote repository.`;
  assert.equal(extractCodebaseJwt(denied), null);
});

test("只有 endpoint 头、没有 JWT 头时返回 null", () => {
  assert.equal(extractCodebaseJwt("X-Code-API-Endpoint https://code.byted.org/api/v2/"), null);
});

test("空输出返回 null", () => {
  assert.equal(extractCodebaseJwt(""), null);
  assert.equal(extractCodebaseJwt("\n\n"), null);
});

test("不会把相邻字段误当成 JWT 的一部分", () => {
  const jwt = extractCodebaseJwt(REAL_OUTPUT);
  assert.ok(jwt);
  assert.ok(!jwt!.includes("Exit"), "抓进了后面的行");
  assert.ok(!/\s/.test(jwt!), "JWT 里不该有空白字符");
});

test("抠出来的 JWT 能被既有的解析链接受", () => {
  const jwt = extractCodebaseJwt(REAL_OUTPUT)!;
  // parseTraeToken 是所有来源共用的下一环，裸 JWT 应原样通过
  assert.equal(parseTraeToken(jwt), jwt);
  // exp=1787424244 → 2026-08-23，能被正确解析而不是落到 10 分钟 fallback
  assert.equal(tokenExpiry(jwt), 1787424244 * 1000);
});
