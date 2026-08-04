# Ghostty setup

```bash
brew install --cask ghostty
```

```bash
mkdir -p ~/.config/ghostty && cp ghostty/config ~/.config/ghostty/config
```

Open Ghostty. If it starts, you're done — the config is live. Verify it parsed:

```bash
ghostty +validate-config
```

## Picking a theme

Ghostty ships ~380 themes. Browse them live, arrow keys to preview:

```bash
ghostty +list-themes
```

The name in the left column is exactly what goes after `theme =`. The config
uses the `light:X,dark:Y` form, so it follows your macOS appearance
automatically — worth keeping, since your flow maps are light and most
terminals are dark.

Four worth previewing first:

| Theme pair | Character |
|---|---|
| `catppuccin-latte` / `catppuccin-mocha` | Soft pastels, the cutest of the four, best light variant. The default here. |
| `rose-pine-dawn` / `rose-pine` | Muted, warm, pink-leaning. Very easy on the eyes over long sessions. |
| `everforest-light-med` / `everforest-dark-med` | Warm greens, low contrast, gentle. |
| `GitHub` / `tokyonight-storm` | Crisp and neutral. Closest to your flow maps' feel. |

If a name errors, run `+list-themes` and copy it exactly — capitalization and
hyphens vary between families.

## Fonts

The config lists three families as a fallback chain, so a missing one is
harmless. To use something else:

```bash
ghostty +list-fonts
```

If you want the icons in Maestro's agent tree and status line to render, use a
Nerd Font patched family:

```bash
brew install --cask font-jetbrains-mono-nerd-font
```

## What replaces the Warp habits

| Warp | Ghostty |
|---|---|
| Blocks — jumping between commands | `Cmd+Shift+↑` / `Cmd+Shift+↓` (shell integration marks each prompt) |
| Command palette | `Cmd+Shift+P` |
| Search output | `Cmd+F` |
| Warp Drive workflows | [Atuin Desktop](https://github.com/atuinsh/desktop) — runbooks that actually run |
| History search | [Atuin](https://atuin.sh) — `Ctrl+R`, synced and encrypted |
| Fuzzy everything | `fzf` — `Ctrl+T` files, `Alt+C` cd |
| Directory jumping | `zoxide` |
| Prompt info | `starship` |

```bash
brew install atuin fzf zoxide starship
```

## Maestro shortcuts in this config

| Key | Does |
|---|---|
| `Cmd+Shift+B` | Opens the board |
| `Cmd+Shift+A` | `claude agents` |
| `Cmd+Shift+T` | `/tree` |
| `Cmd+D` / `Cmd+Shift+D` | Split right / down |
| `Cmd+Shift+Enter` | Zoom the focused split |
| `Cmd+Shift+S` | Dump scrollback to a file and open it |
| `Cmd+\`` | Quick drop-down terminal, from anywhere |

## Inline screenshots

Ghostty speaks the Kitty graphics protocol, so images render at full colour:

```bash
brew install --cask kitty
```

```bash
kitten icat .claude/maestro/shots/login-desktop.png
```

Claude Code itself never renders images inline — it always describes them as
text — so this is for your own review. The board shows the same screenshots at
full size in the browser, which is usually the nicer surface for comparing two.

## One caveat

Ghostty is not a supported backend for Claude Code's split-pane *agent teams*.
That only affects Agent Teams, which Maestro deliberately doesn't use — teams
cap at one level of nesting, subagents give you five. If you ever want to try
teams, run them in the default `in-process` mode, which works in any terminal.
