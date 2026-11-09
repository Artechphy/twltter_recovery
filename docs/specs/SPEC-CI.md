<!-- harness-spec: {"id":"SPEC-CI","status":"draft"} -->
# Public synthetic CI fixture

## 目标

Exercise harness structure on public sample data; this is not an accepted X product specification.

## 参与者与权限

Fixture author/reviewer names are synthetic roles, not real review or approval claims.

## 前置条件

Python 3.11 and Git; runtime uses the standard library.

## 主流程

Run harness lint and its real unit tests, then verify the checked-out GitHub merge candidate.

## 异常与边界

Broken links, invalid task metadata, incorrect checkout SHA and unrelated ancestry fail.

## 状态与一致性

CI fixture stays ready; no production task or private closure evidence is represented.

## 验收场景

Lint succeeds on this fixture; the suite independently checks invalid and missing evidence.

## 未决问题

No product policy is decided by this fixture. Real project lint requires the full authorized project data.

## 非目标

No product implementation, database design, secrets, private reports, or deployment.
