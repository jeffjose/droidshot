import { unzipSync, strFromU8 } from 'fflate';

export interface Display {
  w: number | null;
  h: number | null;
  density: number | null;
  refreshHz: number | null;
}

export interface Device {
  serial: string;
  oem: string;
  model: string;
  android: string;
  sdk: string;
  build: string;
  display: Display;
}

export interface ScrollInfo {
  y: number; // px scrolled from the top of this scroll chain
  content: number; // total scrollable content height (px)
  viewport: number; // visible height (px)
  chain: string; // id of the chain's first node
}

export interface ScreenNode {
  id: string;
  screenshot: string;
  hierarchy: string | null;
  hierarchySource?: 'uiautomator' | 'viewtree' | null;
  scroll?: ScrollInfo;
  activity: string | null;
  capturedAt: string;
}

export interface Action {
  type: 'tap' | 'swipe' | 'key' | 'back';
  x?: number;
  y?: number;
  elementId?: string | null;
  bounds?: [number, number, number, number] | null;
}

export interface Edge {
  from: string;
  to: string;
  action: Action;
  transition: string | null;
}

export interface Manifest {
  formatVersion: number;
  tool: string;
  capturedAt: string;
  device: Device;
  app: unknown | null;
  nodes: ScreenNode[];
  edges: Edge[];
}

export const SUPPORTED_FORMAT_VERSION = 1;

export interface Droidshot {
  manifest: Manifest;
  files: Record<string, Uint8Array>;
}

/** Open a .droidshot (zip) from raw bytes. Throws on bad/unsupported files. */
export function openDroidshot(bytes: Uint8Array): Droidshot {
  const files = unzipSync(bytes);
  const manifestBytes = files['manifest.json'];
  if (!manifestBytes) throw new Error('not a .droidshot: manifest.json missing');
  const manifest = JSON.parse(strFromU8(manifestBytes)) as Manifest;
  if (manifest.formatVersion > SUPPORTED_FORMAT_VERSION) {
    throw new Error(
      `unsupported formatVersion ${manifest.formatVersion} (viewer supports ${SUPPORTED_FORMAT_VERSION})`
    );
  }
  return { manifest, files };
}

const MIME: Record<string, string> = {
  png: 'image/png',
  jpg: 'image/jpeg',
  mp4: 'video/mp4',
  xml: 'application/xml'
};

/** Object URL for an asset referenced in the manifest. Caller revokes it. */
export function assetUrl(ds: Droidshot, path: string): string {
  const bytes = ds.files[path];
  if (!bytes) throw new Error(`asset missing: ${path}`);
  const ext = path.split('.').pop() ?? '';
  return URL.createObjectURL(new Blob([bytes], { type: MIME[ext] ?? 'application/octet-stream' }));
}

export function assetText(ds: Droidshot, path: string): string {
  const bytes = ds.files[path];
  if (!bytes) throw new Error(`asset missing: ${path}`);
  return strFromU8(bytes);
}

export interface HierNode {
  bounds: [number, number, number, number]; // x1,y1,x2,y2 in device px
  cls: string;
  resourceId: string;
  text: string;
  contentDesc: string;
  clickable: boolean;
}

const BOUNDS_RE = /\[(\d+),(\d+)\]\[(\d+),(\d+)\]/;

/** Parse a uiautomator XML dump into a flat list of nodes with bounds. */
export function parseHierarchy(xml: string): HierNode[] {
  const doc = new DOMParser().parseFromString(xml, 'application/xml');
  const out: HierNode[] = [];
  for (const el of Array.from(doc.querySelectorAll('node'))) {
    const m = BOUNDS_RE.exec(el.getAttribute('bounds') ?? '');
    if (!m) continue;
    out.push({
      bounds: [+m[1], +m[2], +m[3], +m[4]],
      cls: el.getAttribute('class') ?? '',
      resourceId: el.getAttribute('resource-id') ?? '',
      text: el.getAttribute('text') ?? '',
      contentDesc: el.getAttribute('content-desc') ?? '',
      clickable: el.getAttribute('clickable') === 'true'
    });
  }
  return out;
}
