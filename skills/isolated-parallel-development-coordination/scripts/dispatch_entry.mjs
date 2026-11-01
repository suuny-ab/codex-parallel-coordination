// Read-only loader for a host-selected, reviewed installation binding.
import {readFile, realpath} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';
import path from 'node:path';

const scripts = 'skills/isolated-parallel-development-coordination/scripts/';

export async function loadInstalledDispatch(installation) {
  // The binding is trusted host sideband after independent review and install;
  // task/request JSON cannot select it or certify its own installation.
  if (!installation?.source_head || !path.isAbsolute(installation.source_root ?? '') ||
      !installation.files?.[scripts + 'dispatch_entry.mjs'] ||
      !installation.files?.[scripts + 'dispatch_host.mjs'] ||
      !installation.files?.[scripts + 'standard_dispatch.py']) {
    throw new Error('reviewed installed source path and complete fingerprints required');
  }
  const root = await realpath(installation.source_root);
  const ownRoot = await realpath(fileURLToPath(new URL('../../../', import.meta.url)));
  if (root !== ownRoot) throw new Error('loader differs from bound installation');
  for (const [relative, expected] of Object.entries(installation.files)) {
    const target = path.resolve(root, relative);
    if (path.isAbsolute(relative) || !target.startsWith(root + path.sep) ||
        await realpath(target) !== target || !/^[a-f0-9]{64}$/.test(expected)) {
      throw new Error('unsafe installed source binding');
    }
    const actual = createHash('sha256').update(await readFile(target)).digest('hex');
    if (actual !== expected) throw new Error('installed source fingerprint mismatch: ' + relative);
  }
  return import(pathToFileURL(path.join(root, scripts, 'dispatch_host.mjs')).href +
    '?sha256=' + installation.files[scripts + 'dispatch_host.mjs']);
}
