# Chess Library (frontend)

A client-side, single-file chess PGN viewer/library for personal OTB (over-the-board)
games. No build step, no framework — one `index.html` with inline `<style>`/`<script>`.

**This file's location moved.** The frontend used to be a separate repo (`chess-library`)
deployed to GitHub Pages. It's now served directly by `chess-library-api` (this repo's
parent directory) as `static/index.html` / `static/dev/index.html`, because GitHub Pages
can only serve pre-existing static files and that made per-game WhatsApp link previews
(a real board image, real player names, per link) fundamentally impossible — see the
parent `CLAUDE.md`'s "Static frontend + single-game sharing" section for the full
rationale and the backend side of that feature. Ships a mobile-first card/bottom-sheet UI
alongside the original desktop table, a light/dark theme, editing tools (single + bulk
game-metadata edit, per-move comment editing, a native date picker), username/password
accounts, and single-game sharing with real per-game link previews.

## Development workflow (important — follow this, don't edit static/index.html directly)

The actual working copy is **`/Users/harman.singh/workspace/chess-analysis/library.html`**,
not this file directly. Workflow for any change:

1. Edit `chess-analysis/library.html`.
2. Sanity check: `node -e '...new Function(script)...'` (extract the `<script>` block and
   confirm it parses).
3. For anything behavioral, write a quick Node test using a minimal DOM stub (see below)
   or a real Playwright screenshot/emulation for visual changes. This project has been
   tested this way all along — don't skip it just because it's a "small" change.
4. Copy to **`chess-library-api/static/dev/index.html`** (staging path, live at
   `https://library.chessscenes.com/dev/`) first, syntax-check the copy, commit, push to
   `chess-library-api`. Let the user review on staging (including on their actual phone)
   before promoting.
5. Only after explicit approval, promote to production: copy to
   **`chess-library-api/static/index.html`**, syntax-check, commit, push. Run a full
   regression pass (desktop + real mobile emulation, not just a resized viewport — see
   the viewport-meta-tag lesson below) immediately before this push, since this is the
   file real users see.
6. Railway auto-deploys `chess-library-api` on push to `main` — **this now deploys the
   frontend too**, since it's the same repo/service. A push here restarts the whole
   process (API included), unlike the old GitHub Pages setup where frontend and backend
   deployed completely independently. Don't be surprised by a brief gunicorn restart on
   a pure-frontend change.

**Why the dev/ split exists**: the user explicitly rejected a separate repo for staging
("I don't like the idea of a separate repo at all"), so staging is a subpath of the same
deployment instead. `static/dev/index.html` is allowed to be iterated on more freely;
`static/index.html` (prod) is the one real users are on and needs the full regression pass
before every push. **This applies to visual/content decisions too, not just code risk** —
a push to prod containing new, never-reviewed copy or design (e.g. a first-draft favicon,
an OG-image tagline nobody had seen yet) has been auto-blocked by the environment's own
safety classifier mid-session for exactly this reason. Stage new copy/visual work, show it
to the user, get explicit sign-off, *then* promote — even for things that feel "obviously
fine."

Node test harness pattern used throughout: stub `document.getElementById`, `localStorage`,
`window` (needed for `window.addEventListener`/`matchMedia`/`scrollTo`), `history`,
`location` (needed since `STORAGE_KEY` and the API path both read `location.pathname`),
`navigator` (needed for `navigator.vibrate` used by the mobile long-press haptic), etc.,
concatenate test code onto the extracted `<script>` source, and `eval()` the whole thing as
one string (top-level `let`/`const` inside a bare `eval()` call don't leak out to separate
`eval()` calls in Node — build one combined script string, not multiple `eval()`s).

For anything mobile-specific, **use Playwright's real device emulation
(`context = await browser.newContext({ ...devices['iPhone 13'] })`), not just
`viewport: {width, height}`.** A plain resized viewport does not reproduce mobile browsers'
"virtual viewport" behavior — see the viewport meta tag bug below, which every
manually-resized-viewport test missed entirely. Playwright itself isn't installed as a
direct dependency in either project directory; `/Users/harman.singh/workspace/boardscan/node_modules`
has it — run test scripts with `NODE_PATH=/Users/harman.singh/workspace/boardscan/node_modules
node your_script.js`.

## Architecture / key concepts

- **All state is client-side.** `games` array + `playerName` + `lightModeEnabled` persisted
  to `localStorage` under `STORAGE_KEY`, gated by `SCHEMA_VERSION` — bump this constant
  whenever the *meaning* of a stored field changes, not just when a field is added/removed.
  This bit us once already: renaming `classifySpeed()`'s `"other"` return value to
  `"untimed"` without bumping the version meant every already-cached session had games with
  the stale value baked in, and the new "Untimed" filter matched zero of them for anyone
  with an existing session.
  `STORAGE_KEY` is also namespaced by path (`"chessLibraryData" + (location.pathname
  .startsWith("/dev/") ? "_dev" : "")`) — `localStorage` is scoped per-*origin*, not per-path,
  so staging and production were silently sharing the same browser storage key despite being
  served from different URLs, until this was namespaced.
  **A shared single game visited via `/g/<id>` deliberately does NOT persist to
  `STORAGE_KEY`** (`loadGames`'s `skipPersist` param, gated in `init()`'s `/g/<id>` code
  path) — this was a real bug caught while building the sharing feature: the older
  `?lib=<id>` "restore my own library on a new device" flow always called
  `saveToStorage()` unconditionally, which is fine for "my own" data but would silently
  overwrite a *different* person's already-saved library if they opened someone else's
  shared single-game link on a device where they already use this app. Don't merge these
  two code paths back together.
- **Columns are fully dynamic.** `discoverColumns()` inspects whatever PGN header tags are
  actually present across loaded games and builds the column list from that.
  `columnState.order` + `columnState.visible` control display; users can show/hide/reorder
  via the Columns panel (floating icon over the table's top-right corner, drag-and-drop
  reordering via native HTML5 DnD events).
- **Sorting**: by PGN Date descending, then **Round descending as a tiebreaker**
  (`sortGamesByDate()` → `dateSortKey()` / `roundSortKey()`), so same-day informal games can
  be sequenced via the Round field. Re-applied on every load/add/restore/edit. `idx` is
  reassigned sequentially after each sort — not stable across sorts or edits, just a
  display/DOM-tracking number. Any edit that can change a game's Date or Round must re-sort
  and must not assume the previously-open detail view's `idx` is still valid afterward (see
  the game-edit modal's handling of this below).
- **Speed classification (`classifySpeed`) has a unit-ambiguity heuristic — read this
  before touching it.** Lichess/chess.com always store PGN `TimeControl` in seconds
  (`"1500+10"` = 25 min), and because their own UIs only offer whole-minute base times, that
  value is always an exact multiple of 60. Some OTB/manual PGN sources instead write the
  base time directly in minutes (`"90+30"` meaning 90 min, not 90 sec) — a different,
  equally valid convention, but if naively divided by 60 it turns a 90-minute classical game
  into "1.5 minutes" and misclassifies it as blitz. Fixed by checking `raw % 60`: if it's
  not an exact multiple of 60, treat `raw` as already being in minutes. This exactly
  explained a real bug report ("Blitz shows 53 games, I know of only 1") down to the exact
  count. Games with no parseable `TimeControl` at all (missing tag, `"-"`, `"?"`, etc.)
  classify as `"untimed"`, with its own filter chip alongside Blitz/Rapid/Classical.
- **Toolbar layout** uses `flex-direction: row-reverse` deliberately (not a media query)
  for structural (not override-based) responsive reordering on desktop widths. On mobile
  (`≤700px`) a separate, more aggressive layout takes over — see the mobile section below.
  **`.search-wrap` must stay `flex:1 1 8rem` with no `max-width`** — it's the toolbar's only
  flex-growing child, so it absorbs leftover space on wide screens; capping its own width
  (a past attempt to fix the clear-button anchoring, see below) starves that role and
  collects the leftover space as a visible gap on the left instead (row-reverse packs
  content toward the visual right by default). The clear-button anchoring problem is solved
  a layer down instead: a `.search-inner` wrapper (position:relative, `max-width:18rem`)
  holds the icon/input/clear-button group and is what the absolutely-positioned clear
  button actually anchors to, while `.search-wrap` itself stays free to grow.
- **Selection mode is a single neutral state, not per-action sub-modes.** `selectionMode`
  is just a boolean. The *only* toolbar entry point is `#enterSelectionBtn` ("Edit"), which
  reveals `#bulkEditBtn` ("Edit", bulk-metadata edit), `#exportBtn`, `#deleteBtn`, and
  `#cancelSelectionBtn` all together — none of them lock you into a committed "export mode"
  or "delete mode" the way they used to. `#cancelSelectionBtn` exits with zero side effects
  (no download, no deletion) — this replaced an earlier design where the *only* way out of
  an accidentally-entered selection was to click Export or Delete again, which could trigger
  an unwanted download just to back out. Mobile long-press on a card and the mobile
  "select all" checkbox flow are additional entry points into the same shared state.
  **Shift-click range selection** works on both the desktop table (`lastCheckedRowIdx`) and
  mobile cards (`lastCheckedMobileIdx`): click one checkbox, shift-click another further up
  or down the *currently rendered/filtered* list, and everything between gets selected —
  scoped to DOM position in the current view, not the underlying game `idx`, so it does the
  right thing under an active filter or search. Both anchor variables reset on
  `exitSelectionMode()` so a fresh session doesn't range back to a stale anchor.
- **Game editing**: `openEditGameModal(g)` (single game, from the pencil icon on a desktop
  expanded row or the mobile sheet) vs `openBulkEditModal(indices)` (multiple selected
  games, from `#bulkEditBtn`) share one modal. Single mode shows White/Black and always
  applies every field (including clearing one intentionally). Bulk mode hides White/Black
  entirely (not meaningful across different games) and treats a **blank field as "leave
  unchanged"** for every other field — Result's blank state is an explicit "— Don't change
  —" dropdown option rather than empty-string-as-a-value, since empty string is itself a
  valid PGN result marker. Saving either mode rebuilds the affected game(s) via the existing
  `buildGameRecord()` so Result/TimeControl edits correctly reclassify `outcome`/`color`/
  `speed`, then re-sorts and refreshes only the currently-open detail view *in place*
  (`refreshOpenDetailViews`) rather than a full `render()`, so the row stays expanded / the
  mobile sheet stays open across an edit. The Date field is a native `<input type="date">`,
  converted to/from PGN's `YYYY.MM.DD` via `pgnDateToIso()`/`isoDateToPgn()` —
  `pgnDateToIso()` validates the date is a real, fully-specified calendar date via a
  round-trip through `Date.UTC` before accepting it, so PGN's partial/unknown dates (e.g.
  `"????.??.??"`) just leave the picker blank instead of crashing or showing garbage.
- **Per-move comment editing**: `tokenizeMovetext()` is the single source of truth for
  parsing PGN movetext into typed tokens (move number / comment / SAN move), used by both
  `formatMovesWithComments()` (rendering, tags each SAN span with `data-game-idx`/
  `data-move-idx` so it's clickable) and `getMoveCommentInfo()`/`applyCommentEdit()` (find
  whether a comment immediately follows a given move, and do the minimal string splice to
  insert/replace/remove it) — rendering and editing can never drift out of sync since they
  share one parser. Click/tap a move → a small modal (matching the game-edit modal's visual
  language) to add, edit, or delete that move's comment; Delete is hidden if there's no
  existing comment. Because desktop *always* renders every game's full move-list HTML into
  the DOM up front (just CSS-hidden until a row is expanded — see mobile section below),
  `refreshOpenDetailViews()` updates *both* the desktop `#pgnrow-<idx>` copy and the mobile
  sheet's copy after an edit, regardless of which one is actually visible.
  **A move pair where both plies have their own comment renders as two separate lines**
  (matching Lichess/chess.com), not one line followed by both comments stacked — see the
  `splitRows` post-processing pass in `formatMovesWithComments()`. Pairs with only one
  commented ply stay combined (already unambiguous as-is). The board widget's currently
  active move also subtly highlights its own comment block (`.move-comment.cw-active`,
  toggled in `highlightMove()` alongside the existing move-token highlight) via a `data-ply`
  attribute on each `.move-comment` div.
- **Board widget** (`createChessWidget`): a hand-rolled board, not an embedded Lichess
  iframe (an earlier version did embed Lichess — that's gone; ignore any stale reference to
  it elsewhere). Pieces are inline SVG data URIs (`PIECE_SVG`, cburnett-style, 12 entries)
  positioned via absolute CSS percentages (`pctPos`/`colFor`/`rowFor`, which also encode
  board orientation). Square colors: light `#8ca2b4`, dark `#5f7891` (`.cw-sq.light`/
  `.cw-sq.dark`) — **the backend's share-preview PNG renderer duplicates these exact values
  and rasterizes these exact 12 SVGs to match**; if you ever change the board's colors or
  piece set here, the backend's preview image will silently drift out of sync unless updated
  to match (see the parent `CLAUDE.md`'s sharing section). **Press `x` (or `X`) while the
  board has focus to flip orientation** — `flipBoard()` is shared between the flip button's
  click handler and the `boardWrap` keydown handler, scoped the same way the existing
  arrow-key ply navigation already was (doesn't interfere with typing elsewhere, e.g. the
  search box, since keydown only fires when the board subtree actually has focus).
- **Analyse button** is a split control, not a single action: main click replays whichever
  site (chess.com or Lichess) was chosen last (`getAnalysisSite()`/`localStorage`, default
  chess.com), a small chevron opens a picker to choose explicitly. Chess.com has no public
  import API, so that path stays copy-PGN-then-open-blank-analysis-page; Lichess *does* have
  one (`POST https://lichess.org/api/import`), used via a **hidden form `target="_blank"`
  submit**, not `fetch()` — deliberately, since a `fetch()`-then-`window.open()` sequence is
  a well-known pattern browsers often block as not being a "direct" user gesture, and
  reading the 303 redirect's `Location` header back in JS hits CORS header-exposure limits
  anyway. The form submit sidesteps both problems and is also just simpler.
- **Accounts (username/password, replacing "Save My Library" as the primary flow)**:
  calls the `chess-library-api` backend's `/api/auth/*` and `/api/library/mine` endpoints
  (see the parent `CLAUDE.md` for the full endpoint list and auth design notes — same repo
  now, not a separate one).
  A `#accountBtn` lives in `.page-header`, **outside `#app`/`#landing`**, so it's visible
  whether or not any games are currently loaded — it was originally nested inside the
  in-app toolbar (like the old Save My Library button was) but that meant a returning
  logged-in user with no local data yet had no way to log in, since the toolbar only
  renders once games are loaded. Don't renest it back inside `#app`.
  **This same button is also the "Save My Library"/single-source entry point**: it reads
  "Log In" when no library is loaded yet, "Save My Library" once one is (clicking generates
  an anonymous link immediately, one click, matching the original flow's muscle memory —
  no separate chooser screen), or the username once logged in. The resulting link modal has
  a "Prefer an account? Log In / Sign Up" footer that pivots straight into the auth modal
  without losing the currently-loaded library. This was a deliberate merge — an earlier
  version of the auth rollout had a *separate* "Log In" button that fully replaced the old
  save-link flow, which the user explicitly rejected: anonymous, no-login usage (including
  getting a one-off link) had to keep working exactly as before, just alongside accounts,
  not instead of them.
  Signup/login share one modal (`#authModal`, same `.modal-overlay`/`.modal-card` pattern
  as Add Games/game-edit) toggled between modes via `authMode`/`updateAuthModalMode()`.
  **Signup pushes the current locally-loaded library up to the new account** (if any games
  are loaded); **login pulls the account's saved library down and replaces what's showing**
  — this asymmetry is deliberate: signup is "claim what I already have", login is "give me
  my account's data back". A 404 on login's pull (fresh account, nothing saved yet) is a
  no-op, not a reset — it deliberately does not clear locally-loaded data, to avoid
  destroying an anonymous session someone was just trying out.
  Session token lives in `localStorage` (`chessLibraryAuthToken` / `_dev` suffix, same
  path-based namespacing as `STORAGE_KEY`) and is sent as `Authorization: Bearer <token>`,
  not a cookie. On load, `checkAuthOnLoad()` validates the stored token against
  `/api/auth/me` before trusting it. Every mutation still calls `saveToStorage()` exactly as
  before (instant local save is unchanged/preserved), which now also calls
  `scheduleAuthSync()` — a 1.5s-debounced `PUT /api/library/mine` that only fires when
  logged in, so the account copy stays in sync in the background without blocking the UI or
  hammering the API on every keystroke.
  The **old anonymous unlisted-link flow (`?lib=<id>`, `loadFromRemoteLibrary()`) still
  works and was deliberately kept** for backward compatibility with links already shared.
  **Google Sign-In is stubbed but not wired up**: the backend endpoint
  (`/api/auth/google`) and the frontend's hidden `#authGoogleWrap`/`#googleSignInBtn`
  placeholder exist, but there's no `GOOGLE_CLIENT_ID` yet (needs manual provisioning via
  Google Cloud Console — see the parent `CLAUDE.md`) and the frontend never loads Google's
  Identity Services script or calls `google.accounts.id.initialize()`, since there's nothing
  to configure it with yet. Don't half-wire this further until the Client ID exists.
- **Single-game sharing**: `shareGame(game, btn)` POSTs `buildPgnForGames([game])` to
  `/api/(dev/)games/share`, shows the resulting `/g/<id>` link in `#shareGameModal` (reuses
  `.save-link-box`/`.save-link-actions` styling). Entry points: `.share-game-btn` in the
  desktop expanded row's `.pgnwrap-actions`, and `#sheetShareBtn` in the mobile sheet header
  — both next to the existing edit/copy-PGN buttons, since it's a deliberate per-game
  action, not something needed inline in the dense table. **The share link is served by
  `chess-library-api` itself now** (`GET /g/<id>`), rendered per-request with real per-game
  `<meta og:*>` tags and a board-position preview image — this is what makes a pasted
  WhatsApp link show an actual position/names/event instead of the site's one fixed generic
  preview. See the parent `CLAUDE.md` for the full backend-side design (why it had to move
  off GitHub Pages, the escaping requirement, the piece-image rasterization approach).
  The full regular app is what's shown at `/g/<id>` — not a stripped-down viewer — per
  explicit user direction ("why would it be minimal? it can be the same as what I use").
- **`API_BASE` is `""` (same-origin)**, not a hardcoded cross-origin Railway URL — frontend
  and API are the same process now. If you ever see a hardcoded `chess-library-api.up
  .railway.app` reappear anywhere in this file, that's a regression from before the
  consolidation; it shouldn't be there.
- **Light/dark theme**: every color in the stylesheet is a CSS custom property, defined
  under `:root` (dark, the default) and re-defined under `:root[data-theme="light"]` — never
  hardcode a color directly in a new rule, add/reuse a token instead. The theme toggle
  (`#themeToggle` in Settings) sets `data-theme` on `<html>` via `applyTheme()`, persists
  `lightModeEnabled` alongside the rest of the saved session. A same-value-in-both-themes
  token still needs stating explicitly in both `:root` blocks for clarity (e.g.
  `--date-icon-filter`, which flips a native date-picker icon's CSS `filter` between
  `invert(1) brightness(1.3)` in dark mode and `none` in light, so the icon stays visible
  against either background).
- **Branding**: the favicon and the header logo lockup (`.logo-h1 .logo-dark` /
  `.logo-h1 .logo-light`, theme-toggled the same way as everything else) are inline SVG data
  URIs sourced from `logos/*.svg` (now `static/logos/*.svg`, kept as tracked source files
  even though their content is baked into the HTML as data URIs, not referenced live) —
  encode with `urllib.parse.quote(svg_text)`, never hand-escape. When toggling two
  theme-scoped elements by class, make sure the *toggle* rule is at least as specific as any
  general sizing rule touching the same elements (e.g. `.logo-h1 img{height:...}` will
  silently beat a lower-specificity `.logo-light{display:none}`, showing both variants
  stacked regardless of theme) — scope both to the same specificity, e.g. `.logo-h1
  .logo-dark` / `.logo-h1 .logo-light`.
- **Social preview** (`og:image`/`twitter:image` etc. in `<head>`): the root `/` and `/dev/`
  pages point to the single static `og-image.png` (1200×630, tracked in `static/`) — this
  part is unchanged and still deliberately one fixed image for the site itself. **This is no
  longer true for shared single games**: `/g/<id>` gets its own per-game `<meta>` tags and
  preview image, rendered server-side per request (see the parent `CLAUDE.md`) — don't
  assume "one static set of tags covers every URL" anymore, that assumption is exactly what
  the sharing feature had to break.
- **Demo data** (`DEMO_PGN` constant): four public-domain historical games, verified
  move-by-move against Wikipedia before shipping. If you ever add more demo content, verify
  against a real source first — the user does not trust generated chess content without
  independent checking. (One of these, the Opera Game, uses "Duke of Brunswick and Count
  Isouard" — spelled with "and", not "&" — deliberately, matching Wikipedia's own convention.)

## Mobile-first UI

Below `≤700px` (`@media (max-width:700px)`), the table (`.table-wrap`) is replaced by:

- **Card list** (`#mobileList`, `.mcard`): one card per game (date/event eyebrow, players
  with the user's own name bolded via a substring match, W/L dot + result + move count).
  Cards are `<div role="button" tabindex="0">`, not `<button>` — a real `<button>` cannot
  validly contain the selection checkbox (`.mcard-cb`). **Long-pressing a card (~500ms
  hold, tracked via `touchstart`/`touchmove`/`touchend` with a movement-tolerance check, not
  a native gesture API) enters selection mode and selects that card** — the trailing
  synthetic `click` that follows a long-press is suppressed via a `longPressFired` flag so it
  doesn't immediately toggle the same card back off.
- Desktop *always* renders every game's full move-list HTML into `#pgnrow-<idx>` up front
  during `render()`, regardless of whether that row is currently expanded — the `.open`
  class only controls visibility via CSS. This means `.move-san-editable` spans (and
  anything else scoped by game) exist in the DOM for *every* game simultaneously, both the
  hidden desktop copies and the currently-visible mobile sheet copy — don't assume a
  page-wide `document.querySelectorAll('.some-per-game-thing')` is scoped to "the one
  currently visible"; scope queries to `#pgnrow-<idx>` or `.sheet-moves` explicitly, or
  you'll silently grab a hidden desktop copy instead of the visible mobile one (bit a test
  script during comment-editing development).
- **Full-screen bottom sheet** (`#gameSheet` + `#sheetScrim`) on tapping a card: reuses the
  same hand-rolled board widget (`createChessWidget`) as the desktop expanded row. Layout is
  a **fixed, non-scrolling embed section on top + an independently scrolling move list
  below** (`.sheet-embed-sticky` / `.sheet-moves`, both children of a non-scrolling flex
  `.sheet-body`) — an earlier version had the board scroll away with the move list, making
  it impossible to check the position while reading long annotations.
- Closing the sheet pushes/pops a `history.pushState` entry so the Android/mobile back
  gesture closes the sheet instead of leaving the page; Escape and the scrim also close it.

### Bugs found during real-device review (fixed, useful lessons — read before touching mobile CSS)

- **Missing `<meta name="viewport">` tag** — root cause the first time the user reported
  "staging looks identical to desktop" on a real phone. Without it, mobile browsers render
  at a fake ~980px "virtual viewport" and zoom out, so `@media (max-width:700px)` never
  matches on a real device even though every desktop-machine Playwright test with a
  manually-set narrow viewport "passed." **Check for this tag first on any future "mobile
  styles aren't applying" report.**
- **`.mcard` was missing `box-sizing:border-box`** — its `width:100%` + own padding/border
  defaulted to `content-box`, pushing the card wider than its container. Any element using
  `width:100%` alongside its own padding/border needs an explicit `box-sizing:border-box`.
- **Class name collision**: the mobile card's `<div class="players">` picked up an unrelated
  desktop rule, `.players{white-space:nowrap}` (meant for the *table's* Players *column*),
  purely because both share the literal class name. Reusing a generic class name across the
  desktop and mobile layouts is a real footgun — check for collisions.
- **Scroll-jumps-to-bottom on file upload**: hiding `#stepUpload` (containing the
  just-clicked, currently-*focused* `#continueBtn`) while revealing taller content beneath it
  is a real, reproducible browser quirk (confirmed under Chromium mobile emulation) — jumps
  scroll to the very bottom. Fixed with `document.activeElement.blur()` before hiding, and
  explicit `window.scrollTo(0,0)` at all landing→app transition points. Don't rely on the
  browser "doing the right thing" here — force it.
- **Dropdown panels anchored to their trigger button** (Settings, Columns, Account) could
  overflow the viewport edge on narrow screens. Fixed via `positionPanelMobile(panel,
  trigger)`, called only under `window.matchMedia("(max-width:700px)")`, switching the panel
  to `position:fixed` with `top` computed from the trigger's `getBoundingClientRect()` and
  `left/right: 1rem` pinned to the viewport. (Save My Library and game-edit are true
  centered modals instead, sidestepping this class of bug entirely.)
- **A floating dropdown panel positioned outside its trigger's own stacking context can get
  painted over by an unrelated `position:sticky` ancestor with a higher `z-index`** — the
  account panel (moved to a persistent `.page-header`, outside the sticky toolbar) rendered
  with its middle section invisible because `.toolbar` is `position:sticky;z-index:15` and
  the panel was only `z-index:10`. Fixed by giving floating panels like this a comfortably
  higher `z-index` (20+) than anything they might visually overlap, not just "higher than
  its own siblings."

## Domain / hosting

`library.chessscenes.com` — **mid-migration as of this writing**: DNS still points at the
old GitHub Pages site; the actual app now lives in `chess-library-api` (this repo) and is
fully live at its own Railway-provided URL, verified working end-to-end, just not yet
reachable via the real domain. See the parent `CLAUDE.md`'s "Custom domain" note under
Deployment for the exact remaining steps (both require the user's own Railway/Namecheap
logins). Once cut over: the old `chess-library` GitHub repo and its Pages deployment get
retired/archived — this repo becomes the sole source of truth for both frontend and API.

## Incidents worth remembering

**An unexpected background push to production.** Mid-session, a stray task-notification
referenced a background agent the current session had not spawned, claiming a feature had
been "pushed and deploying now." A real, unreviewed commit had landed directly on the prod
file, bundling that feature together with in-progress work from *this* session — almost
certainly because both processes were editing the same underlying `chess-analysis/
library.html` concurrently, and the other process's "copy to prod and push" swept up
whatever was in the shared file at the time. First response was to revert the entire
commit — **itself a mistake**, since the bundled feature turned out to be something the
user had separately, legitimately asked for elsewhere. The fix was to isolate just the
unreviewed part, re-apply the wanted part alone, and ship them separately. **Lessons**: (1)
an unexpected prod push is worth reverting immediately as a safety default, but (2) don't
assume the whole change was unwanted just because it wasn't requested in *this*
conversation — confirm before treating a revert as final, and (3) be aware other concurrent
processes may be editing the same shared working file — diff the live site against your own
mental model before assuming you know what's on prod.

**The environment's own push-safety classifier caught a real lapse.** A `git push` that
bundled a brand-new, never-shown-to-the-user OG-image design and tagline copy straight to
production was auto-blocked, correctly, before it executed — nothing was actually pushed
despite the commit/push commands having been issued. The right move (taken after the
block): show the user the actual rendered image and the exact proposed copy, get explicit
sign-off, *then* push — don't treat "this seems obviously fine and low-risk" as a
substitute for that, even for something as small as a tagline.

**A half-thought-through architecture proposal got (correctly) rejected.** An early design
for single-game sharing had the backend publish real static files into the frontend's
public GitHub repo via commits, on every share, as a way to get a fresh per-game URL out of
a static host. The user called this out immediately and directly: treating a public
source-code repository as a permanent, publicly-visible database for other people's
runtime content was the wrong instinct, full stop — not a tradeoff to accept. The actual
fix wasn't a smaller version of that idea, it was recognizing that the *real* constraint
(GitHub Pages can't run code) meant the frontend needed to move to a host that could,
rather than working around the static host's limitation with something increasingly
elaborate. **Lesson**: when a proposed fix only works by repurposing some existing system
for a job it wasn't meant for (a source repo as a database, in this case), that's usually a
sign to question the constraint being routed around, not to polish the workaround.

## Deployment safety

Real users are actively using the live site. Standing agreement: test locally first (syntax
+ functional + visual, using real mobile emulation for anything mobile-facing), stage in
`static/dev/` for anything substantial (a redesign *or* new copy/visual content), get
explicit approval, then promote to `static/index.html`. Don't push half-verified changes,
and don't push unreviewed content/design straight to prod even if it feels safe. Don't
babysit Railway deploys — a `curl` check against the live URL is enough, no retry-looping.

## Things the user has explicitly pushed back on (don't repeat)

- Don't ship copy/content/visual-design decisions (button labels, demo content, a favicon,
  an OG-image tagline) without review first, even if the underlying code is well-tested or
  the change feels small — happened repeatedly across the project's history.
- Don't add a media-query "hack" when a structural CSS fix (e.g., flex order) is possible.
- Don't over-invest in verifying things the user can trivially check themselves.
- Don't assume a resized-viewport Playwright test is equivalent to a real mobile device — it
  misses virtual-viewport behavior entirely (see the viewport meta tag bug above).
- For anything with real stakes (e.g. a one-time bulk cleanup of the user's actual game
  data), don't rely on a single verification pass — independently re-derive and compare the
  specific things that must not change (move sequence, comment text) using a *different*
  code path than the one that did the editing, so a bug in the editor and a matching bug in
  its own self-check can't both be wrong the same way.
- Don't repurpose an existing system for a job outside its actual purpose to route around a
  real constraint (e.g. a public source repo as a database for runtime user content) — fix
  the actual constraint instead, even if that's the bigger-looking change.
- Don't build a "minimal"/stripped-down alternate viewer when the ask is really "the regular
  app, just scoped to less data" — reuse the real thing rather than inventing a second,
  parallel UI to maintain.
