# TodoMVC (third-party test subject, not our work)

This directory is a vendored copy of the plain JavaScript (ES5) TodoMVC example. It is the target app that
Glint is tested against. The Glint team did not write it.

- **Source:** https://github.com/tastejs/todomvc, `examples/javascript-es5/dist/` (master branch download, Nov 21, 2026)
- **License:** MIT, Copyright (c) Addy Osmani, Sindre Sorhus, Pascal Hartig, Stephen Sawchuk. See `LICENSE`.
- **CSS:** `base.css` comes from the `todomvc-common` package and `index.css` from `todomvc-app-css`, both MIT,
  as shipped in the example's `dist/` folder.

## Changes from upstream

One change, so the app runs with no network access:

- `base.js` (from `todomvc-common`) is not included and its `<script>` tag was removed from `index.html`.
  It only adds the todomvc.com "learn" sidebar and Google Analytics, and it requests `learn.json` and the
  GitHub API. The app itself does not depend on it.

All other files are byte-for-byte copies.

## Run

```
python -m http.server --directory apps/todomvc
```
