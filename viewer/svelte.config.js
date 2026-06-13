import adapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import('@sveltejs/kit').Config} */
export default {
  preprocess: vitePreprocess(),
  kit: {
    // Pure client-side SPA: everything runs in the browser, no server needed to view.
    adapter: adapter({ fallback: 'index.html' })
  }
};
