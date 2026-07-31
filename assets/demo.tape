# Regenerates README's demo.gif.  Run it with:  vhs assets/demo.tape
#
# Needs `brew install vhs`, `marshal` on $PATH, and a throwaway repo at /tmp/marshal-demo:
#
#   mkdir -p /tmp/marshal-demo && cd /tmp/marshal-demo && git init -q -b main
#   printf 'def hello(name):\n    return f"Hello, {name}!"\n' > hello.py
#   printf 'clients:\n  deepseek:\n    backend: opencode\n    model: opencode/deepseek-v4-flash-free\n' > fleet.config.yaml
#   git add -A && git commit -qm hello
#
# It is a LIVE run: it spends whatever the client costs and takes as long as the agent takes.
# Any other configured client works - swap `--client deepseek` and the config above. Reset between
# takes with `rm -rf /tmp/marshal-demo/.marshal && git -C /tmp/marshal-demo checkout -- hello.py`.

Output "demo.gif"

Require marshal

Set Shell bash
Set FontSize 17
Set Width 1440
Set Height 760
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

# Setup is not part of the story: a clean prompt, in the demo repo.
Hide
Type "cd /tmp/marshal-demo && export PS1='$ ' && clear"
Enter
Sleep 2s
Show

# 1. Is the fleet actually ready? doctor verifies auth, not just $PATH.
Type "marshal doctor"
Enter
Sleep 7s

Hide
Type "clear"
Enter
Sleep 1s
Show

# 2. Dispatch. Returns immediately; the agent works in its own worktree.
Type "marshal spawn --client deepseek --task-id hello-docstring --goal 'Add a docstring to hello()'"
Enter
Sleep 4s

Type "marshal status"
Enter
Sleep 5s

# The agent is really running - wait for it off-camera rather than filming 90s of nothing.
Hide
Type "while marshal status | grep -q running; do sleep 5; done"
Enter
Sleep 420s
Type "clear"
Enter
Sleep 1s
Show

# 3. Done. Cost provenance is per-backend: this one reports none, so Marshal says
# `unavailable` rather than inventing a number.
Type "marshal status"
Enter
Sleep 6s

# 4. The payoff: real work, sitting in its own worktree. Nothing touched the checkout.
Type "git -C .marshal/worktrees/hello-docstring.*/ diff"
Enter
Sleep 8s
