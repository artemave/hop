import { expect, test } from 'claude-code/testing'

import { linkify } from './register'

const FILES = new Set(['/repo/src/app.ts', '/repo/README.md', '/etc/hosts'])
const exists = async (path: string) => FILES.has(path)
const run = (text: string) => linkify(text, '/repo', exists)

test('a bare path with a line gets a hop link', async () => {
  expect(await run('See src/app.ts:42 for it.')).toBe('See [src/app.ts:42](hop://open/src/app.ts:42) for it.')
})

test('an inline code reference keeps its code styling', async () => {
  expect(await run('Look at `src/app.ts:7`')).toBe('Look at [`src/app.ts:7`](hop://open/src/app.ts:7)')
})

test('a markdown link keeps its label and gets a hop link', async () => {
  expect(await run('[app](./src/app.ts#L3)')).toBe('[app](hop://open/./src/app.ts:3)')
  expect(await run('[readme](README.md)')).toBe('[readme](hop://open/README.md)')
})

test('absolute paths and paths without a line', async () => {
  expect(await run('/etc/hosts and README.md.')).toBe(
    '[/etc/hosts](hop://open//etc/hosts) and [README.md](hop://open/README.md).',
  )
})

test('missing files, urls and fenced code are left alone', async () => {
  const text = 'src/nope.ts:1 https://x.com/src/app.ts e.g. v1.2\n```\nsrc/app.ts:1\n```\n[a](https://x.com)'
  expect(await run(text)).toBe(text)
})
