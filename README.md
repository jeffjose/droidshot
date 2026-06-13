# droidshot

Capture the UI state of a connected Android device into a single portable file,
then step through it in the browser.

A capture is a `.droidshot` file: a zip holding a `manifest.json` plus the
screenshots and UI hierarchies it references. The viewer opens one fully
client-side and lets you replay the navigation, scroll each screen, drill into
sub-screens, and inspect the view hierarchy.

## Requirements

- [uv](https://docs.astral.sh/uv/) for the capture script (it manages its own
  Python and dependencies).
- A device with USB debugging enabled. adb is downloaded into `./vendor`;
  nothing is installed globally.
- Node and pnpm for the viewer.

## Capture

    ./droidshot.py setup            # download adb into ./vendor
    ./droidshot.py doctor           # list connected devices
    ./droidshot.py capture          # capture the current screen

Drive a flow with ordered actions (each one captures the resulting screen):

    ./droidshot.py capture --reset \
      --do "scroll-to=About phone" \
      --do "tap-text=About phone" \
      --do "swipe=up"

Actions: `tap-id=`, `tap-text=`, `tap=x,y`, `swipe=up|down|left|right`,
`scroll=down|up` (scroll to the end, one viewport per step), `scroll-to=<text>`,
`back`, `key=<code>`. Files land in `captures/`.

    ./droidshot.py annotate captures/foo.droidshot   # recompute scroll metadata

## View

    cd viewer
    pnpm install
    pnpm dev

Open the printed URL and drag a `.droidshot` onto the page.

## Format

The `.droidshot` layout and `manifest.json` schema are documented in FORMAT.md.
