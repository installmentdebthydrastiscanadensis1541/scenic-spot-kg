/**
 * Cloudflare Worker — AutoDL开机触发器
 *
 * 部署方法（免费）：
 * 1. 注册 https://dash.cloudflare.com/ → Workers & Pages → Create Worker（不要选Connect to Git）
 * 2. 粘贴此代码，Deploy
 * 3. 设置环境变量（Settings → Variables）：
 *    AUTODL_API_TOKEN     — 你的AutoDL开发者Token（控制台→设置→开发者Token）
 *    AUTODL_INSTANCE_ID   — 实例UUID（如 f6454295d3-1db02697）
 *    AUTODL_PUBLIC_URL    — AutoDL 6006端口HTTPS映射地址（如 https://u1-xxx.seetacloud.com:8443）
 *    ACCESS_KEY           — 访问密码（用户需带 ?key=xxx 访问，留空不认证）
 * 4. 部署后得到的URL就是给测试者的网址
 *
 * 工作流程：
 * 别人访问 Worker URL（带?key=密码）→ Worker检测实例状态 →
 *   如果已开机 → 302重定向到AutoDL公网地址
 *   如果关机 → 自动开机 → 返回"启动中"页面（自动刷新）
 *   如果余额不足 → 显示余额不足提示
 *
 * 注意事项：
 * - AUTODL_PUBLIC_URL 必须是HTTPS地址（AutoDL端口映射默认提供HTTPS）
 * - Cloudflare Workers无法访问非标准端口的HTTP服务，因此不做健康检查，直接重定向
 * - AutoDL开发者Token需实名认证后获取，过期需重新生成
 *
 * 【当前状态】
 * - 完整实现了自动开机、状态检测、余额不足提示、GPU繁忙提示、重定向等功能
 * - 已在实际部署中验证可用
 * - 冷启动（关机→服务就绪）约需3-5分钟
 *
 * 【未来改进方向】
 * - 增加WebSocket/SSE实时推送启动进度，替代页面自动刷新
 * - 支持多实例负载均衡（高峰期自动扩容）
 * - 加入访问统计和费用统计面板
 * - 支持用户排队机制（多人同时访问时依次服务）
 * - 集成Cloudflare D1数据库持久化访问记录
 */

const AUTODL_API = 'https://www.autodl.com/api/v1';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 访问认证
    if (env.ACCESS_KEY) {
      const key = url.searchParams.get('key') || request.headers.get('X-Access-Key') || '';
      if (key !== env.ACCESS_KEY) {
        return new Response('访问被拒绝，请提供正确的key', { status: 403 });
      }
    }

    // 查询实例状态
    try {
      const instanceInfo = await getInstanceStatus(env);
      const status = instanceInfo?.status || 'unknown';

      if (status === 'running' || status === 'Running' || status === '使用中') {
        if (env.AUTODL_PUBLIC_URL) {
          // 不在Worker端做健康检查（Cloudflare访问AutoDL HTTPS有SSL证书问题）
          // 改为返回带JS的等待页面，由用户浏览器检测服务就绪后跳转
          return healthCheckPage(env.AUTODL_PUBLIC_URL);
        }
        return startingPage(env, '实例已开机，但未配置公网地址', 10);
      }

      // 实例关机 → 触发开机
      if (status === 'stopped' || status === 'closed' || status === '已关机' || status === 'offline' || status === 'shutdown') {
        // 先查余额
        const balance = await checkBalance(env);
        if (balance >= 0 && balance < 5) {
          return errorPage('余额不足', `当前余额 ¥${balance.toFixed(2)}，不足以开机。请充值后重试。`);
        }

        // 开机
        const powered = await powerOn(env);
        if (powered === true) {
          return startingPage(env, '正在开机并启动服务，预计需要2-5分钟...', 15);
        } else if (powered === 'gpu_busy') {
          return errorPage('GPU资源繁忙', '当前没有空闲GPU可分配，请稍后再试。按量计费实例的GPU会在关机后释放，重新开机时需等待空闲GPU。');
        } else {
          return errorPage('开机失败', '无法启动AutoDL实例，请检查配置或稍后重试。');
        }
      }

      // 开机中
      if (status === 'starting' || status === '开机中') {
        return startingPage(env, '实例正在开机中，请稍候...', 10);
      }

      // 未知状态
      return startingPage(env, `实例状态: ${status}，正在检查...`, 10);

    } catch (e) {
      return errorPage('查询失败', e.message);
    }
  }
};

async function getInstanceStatus(env) {
  const resp = await fetch(`${AUTODL_API}/instance`, {
    method: 'POST',
    headers: {
      'Authorization': env.AUTODL_API_TOKEN,
      'Content-Type': 'application/json;charset=UTF-8',
    },
    body: JSON.stringify({
      date_from: '', date_to: '',
      page_index: 1, page_size: 100,
      status: [], charge_type: [],
    }),
  });
  const data = await resp.json();
  if (data.code === 'Success') {
    return data.data?.list?.find(i => i.uuid === env.AUTODL_INSTANCE_ID);
  }
  return null;
}

async function checkBalance(env) {
  try {
    const resp = await fetch(`${AUTODL_API}/user/balance`, {
      headers: { 'Authorization': env.AUTODL_API_TOKEN },
    });
    const data = await resp.json();
    if (typeof data.data === 'number') return data.data;
    if (typeof data.data?.balance === 'number') return data.data.balance;
  } catch (e) {}
  return -1;
}

async function powerOn(env) {
  const resp = await fetch(`${AUTODL_API}/instance/power_on`, {
    method: 'POST',
    headers: {
      'Authorization': env.AUTODL_API_TOKEN,
      'Content-Type': 'application/json;charset=UTF-8',
    },
    body: JSON.stringify({ instance_uuid: env.AUTODL_INSTANCE_ID }),
  });
  const data = await resp.json();
  if (data.code === 'Success') return true;
  // 检测GPU繁忙
  const msg = String(data.msg || data.message || '');
  const gpuKeywords = ['GPU', '显卡', '资源', '空闲', '不足', '繁忙', 'busy', 'no available', 'sold out'];
  const isGpuBusy = gpuKeywords.some(kw => msg.includes(kw));
  return isGpuBusy ? 'gpu_busy' : false;
}

function healthCheckPage(publicUrl) {
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小景 · 服务启动中</title>
<style>
  body { font-family: -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #f5f5f5; margin: 0; }
  .card { background: white; border-radius: 12px; padding: 40px; max-width: 450px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
  .spinner { width: 40px; height: 40px; border: 4px solid #e0e0e0; border-top-color: #1a73e8; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 20px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  h2 { color: #333; margin: 0 0 12px; font-size: 20px; }
  p { color: #666; margin: 0; line-height: 1.6; }
  .tip { margin-top: 16px; font-size: 13px; color: #999; }
  #status { margin-top: 8px; font-size: 14px; color: #1a73e8; }
  .progress-bar { width: 100%; height: 6px; background: #e0e0e0; border-radius: 3px; margin-top: 12px; overflow: hidden; }
  .progress-fill { height: 100%; background: #1a73e8; border-radius: 3px; transition: width 0.3s; }
</style>
</head>
<body>
<div class="card">
  <div class="spinner"></div>
  <h2>小景 · 景点知识助手</h2>
  <p>实例已开机，正在加载AI模型...</p>
  <p id="status">正在检测服务状态...</p>
  <div class="progress-bar"><div class="progress-fill" id="progress" style="width:0%"></div></div>
  <p class="tip">预计需要2-4分钟，服务就绪后将自动跳转，请勿关闭</p>
</div>
<script>
const TARGET = ${JSON.stringify(publicUrl)};
let attempts = 0;
const maxAttempts = 80; // 最多检测80次，每次3秒，共4分钟
const intervalMs = 3000;

async function checkHealth() {
  attempts++;
  const pct = Math.min(Math.round(attempts / maxAttempts * 100), 95);
  document.getElementById('progress').style.width = pct + '%';
  
  if (attempts > maxAttempts) {
    document.getElementById('status').textContent = '等待超时，请刷新页面重试';
    document.getElementById('progress').style.width = '100%';
    document.getElementById('progress').style.background = '#f44336';
    return;
  }
  try {
    const resp = await fetch(TARGET + '/health', { mode: 'cors' });
    if (resp.ok) {
      const data = await resp.json();
      if (data.status === 'ok') {
        document.getElementById('status').textContent = '服务已就绪！正在跳转...';
        document.getElementById('progress').style.width = '100%';
        window.location.href = TARGET;
        return;
      }
    }
    const elapsed = attempts * 3;
    document.getElementById('status').textContent = 'AI模型加载中... 已等待' + elapsed + '秒';
  } catch (e) {
    const elapsed = attempts * 3;
    document.getElementById('status').textContent = '等待服务启动... 已等待' + elapsed + '秒';
  }
  setTimeout(checkHealth, intervalMs);
}
checkHealth();
</script>
</body>
</html>`;
  return new Response(html, {
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

function startingPage(env, message, refreshSeconds) {
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小景 · 启动中</title>
<meta http-equiv="refresh" content="${refreshSeconds}">
<style>
  body { font-family: -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #f5f5f5; margin: 0; }
  .card { background: white; border-radius: 12px; padding: 40px; max-width: 400px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
  .spinner { width: 40px; height: 40px; border: 4px solid #e0e0e0; border-top-color: #1a73e8; border-radius: 50%; animation: spin 1s linear infinite; margin: 0 auto 20px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  h2 { color: #333; margin: 0 0 12px; font-size: 20px; }
  p { color: #666; margin: 0; line-height: 1.6; }
  .tip { margin-top: 16px; font-size: 13px; color: #999; }
</style>
</head>
<body>
<div class="card">
  <div class="spinner"></div>
  <h2>小景 · 景点知识助手</h2>
  <p>${message}</p>
  <p class="tip">页面将自动刷新，请勿关闭</p>
</div>
</body>
</html>`;
  return new Response(html, {
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

function errorPage(title, message) {
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>小景 · ${title}</title>
<style>
  body { font-family: -apple-system, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; background: #f5f5f5; margin: 0; }
  .card { background: white; border-radius: 12px; padding: 40px; max-width: 400px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
  .icon { font-size: 48px; margin-bottom: 16px; }
  h2 { color: #d32f2f; margin: 0 0 12px; font-size: 20px; }
  p { color: #666; margin: 0; line-height: 1.6; }
</style>
</head>
<body>
<div class="card">
  <div class="icon">&#9888;</div>
  <h2>${title}</h2>
  <p>${message}</p>
</div>
</body>
</html>`;
  return new Response(html, {
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}
