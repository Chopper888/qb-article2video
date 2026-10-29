const { unlink } = require('node:fs/promises')
const { join, resolve } = require('node:path')

// A custom Electron distribution retains its demo archive. It is not part of this APP.
module.exports = async function removeElectronDemo(context) {
  const output = resolve(context.appOutDir)
  const expected = resolve(context.packager.projectDir, 'release', 'win-unpacked')
  if (context.electronPlatformName !== 'win32' || output.toLowerCase() !== expected.toLowerCase()) {
    throw new Error('Unexpected packaging output; refusing demo cleanup')
  }
  try {
    await unlink(join(output, 'resources', 'default_app.asar'))
  } catch (error) {
    if (error.code !== 'ENOENT') throw error
  }
}
