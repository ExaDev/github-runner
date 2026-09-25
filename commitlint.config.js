// Conventional-commit enforcement for PR commits and PR titles (see .github/workflows/ci.yml's commitlint job). Mirrors the type set .releaserc.json's commit-analyzer releaseRules use, so a type that fails commit-msg validation can never reach a release decision, and vice versa.
module.exports = {
  extends: ['@commitlint/config-conventional'],
};
