import { unzipSync, strFromU8 } from 'fflate';

/** One captured viewport frame and where it landed in the stitched image. */
export interface LongshotFrame {
  file: string;
  delta: number; // px this frame scrolled past the previous one (0 for frame 0)
  y: number; // cumulative scroll offset in content space
}

/** A join in the stitched image: frame `frame` contributed rows [y, y+h). */
export interface Seam {
  frame: number;
  y: number; // top of the contributed slice, in stitched px
  h: number; // height of the contributed slice, in stitched px
}

export interface LongshotManifest {
  tool: string;
  device: { w: number; h: number };
  viewport: [number, number];
  topChrome: number;
  bottomChrome: number;
  stitched: string;
  stitchedSize: [number, number]; // [w, h]
  seams: Seam[];
  frames: LongshotFrame[];
}

export interface Longshot {
  manifest: LongshotManifest;
  files: Record<string, Uint8Array>;
}

/** Open a .longshot (zip) from raw bytes. Throws on bad files. */
export function openLongshot(bytes: Uint8Array): Longshot {
  const files = unzipSync(bytes);
  const mb = files['manifest.json'];
  if (!mb) throw new Error('not a .longshot: manifest.json missing');
  const manifest = JSON.parse(strFromU8(mb)) as LongshotManifest;
  if (!manifest.frames || !manifest.stitched) {
    throw new Error('manifest is missing frames/stitched');
  }
  return { manifest, files };
}

/** Object URL for a file in the bundle. Caller revokes it. */
export function fileUrl(ls: Longshot, path: string): string {
  const bytes = ls.files[path];
  if (!bytes) throw new Error(`missing in bundle: ${path}`);
  return URL.createObjectURL(new Blob([bytes], { type: 'image/png' }));
}
