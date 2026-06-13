# The `.droidshot` format

A `.droidshot` file is a **ZIP archive** containing one `manifest.json` index and a
set of content-addressed assets. It is the contract between the capture tool
(Python) and the viewer (SvelteKit) — they share no code, only this format.

```
my-capture.droidshot            (a zip)
├── manifest.json               required — the index
└── assets/
    ├── <sha256>.png            screenshots
    ├── <sha256>.xml            UI hierarchy dumps (uiautomator)
    └── <sha256>.mp4            transition clips        (later)
```

Assets are named by the lowercase hex SHA-256 of their bytes. This deduplicates
identical content (revisit a screen → one blob) and makes the file self-verifying.
`manifest.json` references each asset by its relative path, e.g. `assets/<sha>.png`.

## `manifest.json` (formatVersion 1)

```jsonc
{
  "formatVersion": 1,                 // integer; bump on breaking changes
  "tool": "droidshot/0.1.0",          // producer + version
  "capturedAt": "2026-06-13T04:47:00+00:00",  // ISO-8601 UTC

  "device": {
    "serial":  "4B141FDCH0001U",
    "oem":     "Google",
    "model":   "Blazer",
    "android": "15",                  // ro.build.version.release
    "sdk":     "35",                  // ro.build.version.sdk
    "build":   "google/blazer/...",   // ro.build.fingerprint
    "display": { "w": 1080, "h": 2410, "density": 420, "refreshHz": null }
  },

  "app": null,                        // {package, versionName, versionCode} | null

  "nodes": [                          // captured screen states
    {
      "id": "n0",                     // unique within the file
      "screenshot": "assets/<sha>.png",
      "hierarchy":  "assets/<sha>.xml",   // nullable
      "hierarchySource": "uiautomator",   // "uiautomator" (rich, has text) |
                                          // "viewtree" (dumpsys fallback, no text) | null
      "activity":   "com.android.settings/.homepage.SettingsHomepageActivity",
      "capturedAt": "2026-06-13T04:47:00+00:00"
    }
  ],

  "edges": [                          // actions linking one node to the next
    {
      "from": "n0",
      "to":   "n1",
      "action": {
        "type": "tap",                // tap | swipe | key | back  (only tap in M1)
        "x": 540, "y": 1200,          // screen pixels
        "elementId": "com.android.settings:id/...",  // nullable
        "bounds": [40, 1150, 1040, 1260]              // [x1,y1,x2,y2] | null
      },
      "transition": null              // assets/<sha>.mp4 once screenrecord lands
    }
  ]
}
```

### Coordinate system
All pixel coordinates (`action.x/y`, `bounds`, and `bounds` inside the hierarchy
XML) are in **device display pixels** — the `device.display.w × h` space. The
viewer scales them to whatever size it renders the screenshot at.

### Compatibility rules
- A reader MUST check `formatVersion` and refuse versions it doesn't understand.
- Unknown fields MUST be ignored (forward-compatible).
- `nodes[0]` is the entry node for replay unless a future `entry` field says otherwise.
