import net from 'node:net';
import { StringDecoder } from 'node:string_decoder';
import { defineTool } from '@deepseek-ai/dsh-tools';

export const name = 'autoops';
export const inject = ['tools'];

function callBroker(request, signal) {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection('/run/autoops/broker.sock');
    let data = '';
    const decoder = new StringDecoder('utf8');
    let received = 0;
    const stop = () => socket.destroy(new Error('运维调用已取消'));
    signal?.addEventListener('abort', stop, { once: true });
    socket.setTimeout(230000, () => socket.destroy(new Error('运维调用超时')));
    socket.on('connect', () => socket.write(JSON.stringify({ token: process.env.AUTOOPS_TOKEN, request }) + '\n'));
    socket.on('data', chunk => {
      received += chunk.length;
      if (received > 262144) return socket.destroy(new Error('执行器响应过大'));
      data += decoder.write(chunk);
      const end = data.indexOf('\n');
      if (end < 0) return;
      try {
        const reply = JSON.parse(data.slice(0, end));
        if (!reply.ok) reject(new Error(reply.error || '执行器拒绝请求'));
        else resolve(JSON.stringify(reply.result));
      } catch (error) { reject(error); }
      socket.end();
    });
    socket.on('error', reject);
    socket.on('close', () => {
      signal?.removeEventListener('abort', stop);
      if (!data.includes('\n')) reject(new Error('执行器未返回完整响应'));
    });
  });
}

const OPERATIONS = ['inventory', 'inspect', 'logs', 'verify', 'start', 'restart', 'recreate', 'run_action', 'read_config', 'patch_config', 'read_file', 'patch_file', 'finish'];

export function normalizeRequest(args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('参数必须是对象；例如 {"op":"inventory"}');
  if (Buffer.byteLength(JSON.stringify(args)) > 120000) throw new Error('运维请求过大');
  let request = args;
  if (args.request !== undefined) {
    if (Object.keys(args).some(key => key !== 'request')) throw new Error('不能混用平铺参数和旧 request');
    if (typeof args.request !== 'string') throw new Error('旧 request 必须是 JSON 对象字符串');
    try { request = JSON.parse(args.request); }
    catch { throw new Error('request 不是有效 JSON；请直接传 {"op":"inventory"}'); }
  }
  if (!request || typeof request !== 'object' || Array.isArray(request)) throw new Error('request 必须是对象');
  if (!OPERATIONS.includes(request.op)) throw new Error('缺少或无效 op；请直接传 {"op":"inventory"}，不要传空对象');
  const fields = {
    inspect: ['target'], logs: ['target'], verify: ['target'], start: ['target'], restart: ['target'], recreate: ['target'],
    run_action: ['target', 'action'], read_config: ['target', 'config'],
    patch_config: ['target', 'config', 'expected_sha256'], read_file: ['target', 'code_file'],
    patch_file: ['target', 'code_file', 'expected_sha256', 'old_string'], finish: ['summary'],
  };
  for (const key of fields[request.op] || []) {
    if (typeof request[key] !== 'string' || !request[key].trim()) throw new Error(`${request.op} 需要非空字符串 ${key}`);
  }
  if (request.op === 'patch_file' && typeof request.new_string !== 'string') throw new Error('patch_file 需要字符串 new_string');
  if (request.op === 'patch_config' && (!request.changes || typeof request.changes !== 'object' || Array.isArray(request.changes) || !Object.keys(request.changes).length)) throw new Error('patch_config 需要非空对象 changes');
  if (request.notification_summary !== undefined && (typeof request.notification_summary !== 'string' || !request.notification_summary.trim() || [...request.notification_summary].length > 200)) throw new Error('notification_summary 必须是非空字符串，最多 200 字');
  if (request.needs_scan !== undefined && typeof request.needs_scan !== 'boolean') throw new Error('needs_scan 必须是布尔值');
  return request;
}

export function apply(ctx) {
  ctx.tools.guard(exec => exec.name === 'ops' ? undefined : '自动运维仅允许 ops 结构化工具');
  ctx.tools.register(defineTool({
    name: 'ops',
    description: '服务器自动运维的唯一工具。直接传平铺参数，如 {"op":"inventory"}，不要嵌套或编码 JSON。先 inventory 查看允许的 targets/configs/actions/code_files/code_roots，再 inspect/logs/read_config/read_file 查证并积极修复。patch_config 只改许可标量；patch_file 只改获准代码别名或 code_roots 内普通 .py 相对路径（敏感模块和链接除外），均需哈希并由执行器备份验证回滚。finish(summary,notification_summary?,needs_scan?) 基于真实探针结案。notification_summary 用不超过 200 字中文自然语言，第一句先给结论（修好了 / 没修好 / 需要管理员做什么），再补最关键的一句原因；不分章节、不复述工具输出、不罗列探针名，未知就如实说未知。旧 request 字符串仅为兼容保留，不与平铺参数混用。',
    parameters: {
      op: { type: 'string', enum: OPERATIONS, description: '操作名，新调用必须提供；例如 inventory。' },
      target: { type: 'string', description: 'inventory 返回的授权目标名。' },
      action: { type: 'string', description: 'run_action 的授权动作名。' },
      config: { type: 'string', description: 'read_config/patch_config 的配置别名。' },
      code_file: { type: 'string', description: 'read_file/patch_file 的授权代码别名或 code_roots 内普通 .py 相对路径；重名加 deployment/ 或 wechat_export_send/ 前缀。禁止绝对路径、敏感模块和链接。' },
      offset: { type: 'integer', description: 'read_file 续读的非负字节偏移，使用返回的 next_offset。' },
      expected_sha256: { type: 'string', description: '读取文件时获得的当前 SHA256。' },
      changes: { type: 'object', additionalProperties: true, description: 'patch_config 的非空标量更改映射。' },
      old_string: { type: 'string', description: 'patch_file 要唯一匹配的原文。' },
      new_string: { type: 'string', description: 'patch_file 的替换文本，可为空字符串。' },
      summary: { type: 'string', description: 'finish 的中文诊断、已尝试修复及失败原因。' },
      notification_summary: { type: 'string', description: 'finish 时由实际修复 agent 自己写的中文自然语言简要（最多 200 字）：第一句给结论，再补最关键的原因；不复述工具输出，未知就说未知，不含密钥。' },
      needs_scan: { type: 'boolean', description: '仅在需要人工扫码或介入时设 true。' },
      request: { type: 'string', description: '仅兼容旧调用的 JSON 对象字符串；新调用直接使用 op 等平铺参数。' },
    },
    output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      const names = ctx.tools.schemas(exec.agent).map(tool => tool.name).sort();
      if (names.length !== 1 || names[0] !== 'ops') throw new Error('工具清单不符合隔离策略，拒绝运维');
      console.error('AUTOOPS_TOOLS=' + JSON.stringify(names));
      const request = normalizeRequest(args);
      return callBroker(request, exec.signal);
    },
  }));
}
