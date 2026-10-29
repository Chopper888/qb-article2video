// R101 I：渲染（npx remotion render）用的 Node 进程里，TCP 监听一律只开在本机回环地址 127.0.0.1。
//
// Remotion 4 的本地静态服务器绑定 0.0.0.0 / ::（所有网卡），挑端口时还会在 :: 和 0.0.0.0 上试监听，
// Windows 第一次出片因此弹"允许 Node.js 访问网络"的防火墙提示。Remotion 命令行没有只监听回环的选项，
// 引擎通过 NODE_OPTIONS=--require 预加载本文件（见 steps.render_env），把"所有网卡"改成 127.0.0.1。
// 浏览器访问的地址是 http://localhost:<端口>，仍然连得上。具名管道 / Unix 套接字（path）不动。
'use strict'
const net = require('net')

const ANY = new Set(['0.0.0.0', '::', '::0', ''])
const LOOPBACK = '127.0.0.1'

function loopbackHost(host) {
  return host === undefined || host === null || ANY.has(String(host)) ? LOOPBACK : host
}

function rewrite(args) {
  const a = args.slice()
  const first = a[0]
  if (first !== null && typeof first === 'object' && !Array.isArray(first)) {
    if (first.path !== undefined || first.fd !== undefined || first._handle || first.handle) return a
    a[0] = Object.assign({}, first, { host: loopbackHost(first.host) })
    return a
  }
  const isPort = typeof first === 'number' || (typeof first === 'string' && /^\d+$/.test(first))
  if (!isPort && first !== undefined && typeof first !== 'function') return a  // path / handle
  if (first === undefined || typeof first === 'function') {
    a.unshift(0)
  }
  if (typeof a[1] === 'string') a[1] = loopbackHost(a[1])
  else a.splice(1, 0, LOOPBACK)
  return a
}

const listen = net.Server.prototype.listen
net.Server.prototype.listen = function (...args) {
  return listen.apply(this, rewrite(args))
}

module.exports = { rewrite, loopbackHost, LOOPBACK }
