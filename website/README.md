# pair-desk.renner.dev

The project homepage, served by GitHub Pages from this folder. Plain static files: no build step,
no external fonts or scripts, a strict Content Security Policy. `releases.js` asks the public
GitHub API for the newest release to name it in the install section; without it that sentence
stays hidden.

- **The loop** in the hero is an illustration built in HTML, CSS and SVG. The desk card, the tool
  calls and the hook lines use Pair Desk's real wording (`pair_desk/notify.py`, `pair_desk/cli.py`);
  keep them in step when that wording changes. The game is a sketch, and the page says so.
- **Screenshots** in `assets/shots/` are real captures of the web UI with the sample desk from
  `scripts/demo-desk.py`, taken headless at 1360 × 820 in the light and dark theme
  (`<view>-light.webp`, `<view>-dark.webp`). Retake them when the UI changes noticeably, and
  update the version in the note under the viewer.
- **Deploy:** run the **Website Pages** workflow from the Actions tab (manual only). It needs
  GitHub Pages set to "GitHub Actions" in the repository settings and a DNS record for
  `pair-desk.renner.dev` (`CNAME`).
