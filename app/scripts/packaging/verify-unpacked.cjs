const fs = require('node:fs/promises')
const path = require('node:path')
const { createHash } = require('node:crypto')

// Release exceptions are exact paths AND zero-byte source files, never a glob.
const allowedEmptyPlaceholders = new Set([
  'runtime/node_modules/@xtuc/ieee754/dist/.gitkeep',
  'runtime/node_modules/terser/dist/.gitkeep'
])

function compareInventories(source, target) {
  const allowedMissing = [], missing = [], extra = [], changed = []
  for (const [name, entry] of Object.entries(source)) {
    if (!Object.hasOwn(target, name)) {
      if (allowedEmptyPlaceholders.has(name) && entry.bytes === 0) allowedMissing.push(name)
      else missing.push(name)
    } else if (entry.bytes !== target[name].bytes || entry.sha256 !== target[name].sha256) changed.push(name)
  }
  for (const name of Object.keys(target)) if (!Object.hasOwn(source, name)) extra.push(name)
  return { status: missing.length || extra.length || changed.length ? 'FAIL' : 'PASS',
    allowedMissing, missing, extra, changed }
}

async function inventory(root) {
  const entries = Object.create(null)
  async function visit(directory) {
    for (const entry of await fs.readdir(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name)
      if (entry.isSymbolicLink()) throw new Error(`Unsupported link: ${absolute}`)
      if (entry.isDirectory()) await visit(absolute)
      else if (entry.isFile()) {
        const content = await fs.readFile(absolute)
        entries[path.relative(root, absolute).split(path.sep).join('/')] = {
          bytes: content.length, sha256: createHash('sha256').update(content).digest('hex') }
      } else throw new Error(`Unsupported entry: ${absolute}`)
    }
  }
  await visit(root)
  return entries
}

async function verifyUnpacked(sourceRoot, targetRoot) {
  const source = await inventory(sourceRoot), target = await inventory(targetRoot)
  return { sourceRoot, targetRoot, sourceFiles: Object.keys(source).length,
    targetFiles: Object.keys(target).length,
    targetBytes: Object.values(target).reduce((sum, entry) => sum + entry.bytes, 0),
    ...compareInventories(source, target) }
}

module.exports = { compareInventories, verifyUnpacked }
if (require.main === module) {
  const sourceRoot = path.resolve(process.argv[2] || '.cache/runtime-pack-v5')
  const targetRoot = path.resolve(process.argv[3] || 'release/win-unpacked/resources/runtime-pack-v5')
  verifyUnpacked(sourceRoot, targetRoot).then(result => {
    console.log(JSON.stringify(result, null, 2))
    process.exitCode = result.status === 'PASS' ? 0 : 1
  }).catch(error => { console.error(error.message); process.exitCode = 1 })
}
