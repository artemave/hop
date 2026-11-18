import type { Register } from 'claude-code'

const SEG = String.raw`\.?[\w@+-](?:[\w.@+-]*[\w@+-])?`
const PATH = String.raw`(?:\.{1,2}/|/)?${SEG}(?:/${SEG})*`
const LINE = String.raw`(?::(\d+)(?:[:-]\d+)?|#L(\d+)(?:-L?\d+)?)?`

const REF = new RegExp(`^(${PATH})${LINE}$`)
const TOKEN = new RegExp(
  [
    String.raw`\[([^\]\n]*)\]\(([^)\s]+)\)`,
    String.raw`[a-zA-Z][\w+.-]*://[^\s)>\]]+`,
    '`([^`\\n]+)`',
    String.raw`(?<![\w/.:@~-])(${PATH}(?::\d+(?:[:-]\d+)?)?)(?![\w/])`,
  ].join('|'),
  'g',
)
const FENCE = /^\s*(```|~~~)/

type Ref = { path: string; line?: string }

const parseRef = (text: string): Ref | undefined => {
  const [, path, line, hashLine] = REF.exec(text) ?? []
  if (path === undefined || !/[/.]/.test(path)) return undefined
  return { path, line: line ?? hashLine }
}

const absolute = (cwd: string, path: string) => {
  const parts: string[] = []
  for (const part of (path.startsWith('/') ? path : `${cwd}/${path}`).split('/')) {
    if (part === '..') parts.pop()
    else if (part !== '' && part !== '.') parts.push(part)
  }
  return `/${parts.join('/')}`
}

// Same encoding as hop's own `hop_url` (urllib quote, safe='/:'), so hop opens and previews it.
const hopUrl = (selection: string) =>
  `hop://open/${encodeURIComponent(selection).replace(/%2F/g, '/').replace(/%3A/g, ':')}`

const refsIn = (match: (string | undefined)[]): { ref: Ref; render: (href: string) => string } | undefined => {
  const [, label, target, code, bare] = match
  if (target !== undefined) {
    const ref = parseRef(target)
    return ref && { ref, render: href => `[${label}](${href})` }
  }
  if (code !== undefined) {
    const ref = parseRef(code)
    return ref && { ref, render: href => `[\`${code}\`](${href})` }
  }
  if (bare !== undefined) {
    const ref = parseRef(bare)
    return ref && { ref, render: href => `[${bare}](${href})` }
  }
  return undefined
}

const proseChunks = (text: string) => {
  const chunks: { text: string; isCode: boolean }[] = []
  let isCode = false
  for (const line of text.split('\n')) {
    const isFence = FENCE.test(line)
    const lineIsCode = isCode || isFence
    if (isFence) isCode = !isCode
    const last = chunks.at(-1)
    if (last && last.isCode === lineIsCode) last.text += `\n${line}`
    else chunks.push({ text: line, isCode: lineIsCode })
  }
  return chunks
}

export const linkify = async (text: string, cwd: string, exists: (path: string) => Promise<boolean>) => {
  const chunks = proseChunks(text)
  const paths = new Set<string>()
  for (const chunk of chunks.filter(c => !c.isCode)) {
    for (const match of chunk.text.matchAll(TOKEN)) {
      const found = refsIn(match)
      if (found) paths.add(absolute(cwd, found.ref.path))
    }
  }
  const existing = new Set<string>()
  await Promise.all([...paths].map(async path => (await exists(path)) && existing.add(path)))

  return chunks
    .map(chunk =>
      chunk.isCode
        ? chunk.text
        : chunk.text.replace(TOKEN, (...args) => {
            const found = refsIn(args.slice(0, 5))
            if (!found) return args[0]
            if (!existing.has(absolute(cwd, found.ref.path))) return args[0]
            return found.render(hopUrl(`${found.ref.path}${found.ref.line ? `:${found.ref.line}` : ''}`))
          }),
    )
    .join('\n')
}

export const register: Register = on => {
  on('ui.render', { component: 'AssistantMessage' }, async ($, e, next) => {
    const cwd = await $.session.cwd()
    const text = await linkify(e.props.text, cwd, path =>
      $.fs.stat(path).then(
        () => true,
        () => false,
      ),
    )
    return next({ ...e, props: { ...e.props, text } })
  })
}
