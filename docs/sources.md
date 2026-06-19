# Sources — Headless Agent Orchestrator research (2026-06-19)

## OpenCode
- Docs: https://opencode.ai/docs/ (cli, server, permissions, config, models, agents, mcp-servers, zen, go, sdk)
- Repo (MOVED): https://github.com/sst/opencode → 302 → https://github.com/anomalyco/opencode  (npm still `opencode-ai`; SDK `@opencode-ai/sdk`)
- Server: https://opencode.ai/docs/server/  · Permissions: https://opencode.ai/docs/permissions/  · Config: https://opencode.ai/docs/config/
- Models/subs: https://opencode.ai/docs/models/ · Go ($10/mo): https://opencode.ai/docs/go/ · Zen (PAYG): https://opencode.ai/docs/zen/
- Stream-json event schema: https://takopi.dev/reference/runners/opencode/stream-json-cheatsheet/
- Cost tooling: https://ccusage.com/guide/opencode/ · https://github.com/junhoyeo/tokscale · https://github.com/ramtinJ95/opencode-tokenscope
- Key bugs: stream drops final step_finish https://github.com/anomalyco/opencode/issues/26855 · serve+attach "ask" hang https://github.com/anomalyco/opencode/issues/16367 · question tool blocks headless https://github.com/anomalyco/opencode/issues/10012 · hang on API error https://github.com/anomalyco/opencode/issues/8203 · rate-limit no retry https://github.com/anomalyco/opencode/issues/16994 · subagent no timeout https://github.com/anomalyco/opencode/issues/15072

## Cursor CLI
- Headless: https://cursor.com/docs/cli/headless · Using: https://cursor.com/docs/cli/using · Params: https://cursor.com/docs/cli/reference/parameters
- Output format: https://cursor.com/docs/cli/reference/output-format · Config: https://cursor.com/docs/cli/reference/configuration · Permissions: https://cursor.com/docs/cli/reference/permissions
- Security/run-modes: https://cursor.com/docs/agent/security · MCP: https://cursor.com/docs/cli/mcp · GitHub Actions: https://cursor.com/docs/cli/github-actions
- Admin API (usage/cost, team/enterprise): https://cursor.com/docs/account/teams/admin-api · cost guide https://www.vantage.sh/blog/track-cursor-costs · usage MCP https://github.com/ofershap/cursor-usage
- Stream format capture: https://tarq.net/posts/cursor-agent-stream-format/
- Key bugs: -p hang https://forum.cursor.com/t/.../150246 · terminal not released https://forum.cursor.com/t/.../133624 · concurrent fail https://forum.cursor.com/t/.../142677 · zshrc hang https://forum.cursor.com/t/.../107260 · headless trust/MCP https://forum.cursor.com/t/.../135611 · session titles "New Agent" https://forum.cursor.com/t/.../143731

## Prior art (orchestrators)
- AWS CAO (gold standard): https://github.com/awslabs/cli-agent-orchestrator
- shinpr/sub-agents-mcp: https://github.com/shinpr/sub-agents-mcp · skills: https://github.com/shinpr/sub-agents-skills
- Roundtable: https://github.com/askbudi/roundtable · HN https://news.ycombinator.com/item?id=45374908
- cmuxlayer (screen-scrape usage): https://github.com/EtanHey/cmuxlayer
- ORCH (worktree + review SM + cost): https://github.com/oxgeneral/ORCH
- Lists: https://github.com/andyrewlee/awesome-agent-orchestrators · https://github.com/bradAGI/awesome-cli-coding-agents
- Others: agentsmesh, clideck (OpenCode support); Dex; swarm-protocol; crystal; orca; parallel-code; gnap; bernstein; agentbox/agenttier/scion (stronger isolation)

## Abstraction / usage schema reference
- litellm provider abstraction: https://docs.litellm.ai/docs/provider_registration/ · cost tracking: https://docs.litellm.ai/docs/proxy/cost_tracking
- PraisonAI cursor-cli: https://docs.praison.ai/docs/code/cursor-cli
- Claude Code headless SDK: https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-headless (note `-p` empty-result bug #38623)
