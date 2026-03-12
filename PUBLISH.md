# Publish Guide (AstrBot Plugin)

This plugin is prepared for release. Follow these commands to publish to GitHub.

## 1) Initialize repository

```bash
git init
git branch -M main
git add .
git commit -m "release: game_monitor v0.3.1"
```

## 2) Create GitHub repository

Create a new empty repository on GitHub, for example:

- Repository name: astrbot_plugin_game_monitor
- Visibility: Public
- Do not add README/.gitignore/license (already exists locally)

## 3) Connect remote and push

```bash
git remote add origin https://github.com/nerdchan/astrbot_plugin_game_monitor.git
git push -u origin main
```

## 4) Optional release tag

```bash
git tag v0.3.1
git push origin v0.3.1
```

## Notes

- Ensure metadata.yaml `name`/`desc`/`repo` exactly match your marketplace submission JSON.
- Runtime files are ignored:
  - data/state.json
  - data/temp/

## Suggested GitHub Description

Discord game presence monitor for AstrBot with persona-style concern replies. Known issues: persona linkage may fail when persona is managed by other plugins; web-search comments are currently too formal (Google-first), and support for bilibili/X/Steam-style player reviews is planned.
