"""手机网页遥控器（单页，无外部依赖，离线可用）。"""

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1115">
<title>TV投屏 · 手机遥控</title>
<style>
  :root{
    --bg:#0f1115; --card:#171a21; --card2:#1e222b; --line:#262b36;
    --fg:#e8eaed; --dim:#8b93a1; --accent:#4f8cff; --ok:#31c48d; --warn:#f4a13c; --bad:#f05a5a;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       padding-bottom:env(safe-area-inset-bottom)}
  header{position:sticky;top:0;z-index:20;background:rgba(15,17,21,.92);backdrop-filter:blur(12px);
         border-bottom:1px solid var(--line);padding:12px 16px;display:flex;align-items:center;justify-content:space-between}
  header h1{font-size:17px;margin:0;font-weight:600}
  .ip{font-size:12px;color:var(--dim)}
  main{padding:12px;display:flex;flex-direction:column;gap:12px;max-width:720px;margin:0 auto}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
  .card h2{font-size:13px;margin:0 0 10px;color:var(--dim);font-weight:600;letter-spacing:.04em;
           display:flex;align-items:center;justify-content:space-between}
  button{font:inherit;color:var(--fg);background:var(--card2);border:1px solid var(--line);
         border-radius:10px;padding:9px 14px;cursor:pointer;transition:.15s}
  button:active{transform:scale(.97)}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
  button.ghost{background:transparent}
  button:disabled{opacity:.45;cursor:not-allowed}
  .row{display:flex;gap:8px;flex-wrap:wrap}
  .dev{display:flex;align-items:center;gap:10px;padding:10px;border-radius:10px;
       border:1px solid var(--line);background:var(--card2);margin-bottom:8px;cursor:pointer}
  .dev.on{border-color:var(--accent);background:rgba(79,140,255,.12)}
  .dev .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .dev .tag{font-size:11px;padding:2px 7px;border-radius:6px;background:#2a3140;color:var(--dim)}
  .dev.on .tag{background:var(--accent);color:#fff}
  .muted{color:var(--dim);font-size:13px}
  input[type=search]{width:100%;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
                     background:var(--card2);color:var(--fg);font:inherit}
  .crumb{font-size:13px;color:var(--dim);margin-bottom:8px;word-break:break-all}
  .crumb a{color:var(--accent);text-decoration:none;cursor:pointer}
  .item{display:flex;align-items:center;gap:8px;padding:11px 10px;border-bottom:1px solid var(--line);
        cursor:pointer}
  .item:last-child{border-bottom:0}
  .item:active{background:var(--card2)}
  .item .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .item .sz{font-size:12px;color:var(--dim);white-space:nowrap}
  .list{max-height:44vh;overflow:auto;-webkit-overflow-scrolling:touch}
  .now{font-weight:600;margin-bottom:8px;word-break:break-all}
  .bar{width:100%;height:5px;background:#2a3140;border-radius:3px;overflow:hidden}
  .bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .4s linear}
  .times{display:flex;justify-content:space-between;font-size:12px;color:var(--dim);margin-top:6px}
  input[type=range]{width:100%;margin:8px 0;accent-color:var(--accent)}
  .ctl{display:flex;gap:8px;align-items:center}
  .ctl button{flex:1}
  .volrow{display:flex;align-items:center;gap:10px;margin-top:6px}
  .volrow span{font-size:13px;color:var(--dim);min-width:34px}
  .badge{font-size:11px;padding:2px 6px;border-radius:6px;white-space:nowrap;flex:none}
  .badge.ok{background:rgba(49,196,141,.16);color:#5ad9a6}
  .badge.tc{background:rgba(244,161,60,.16);color:#f4b95c}
  .badge.ck{background:rgba(79,140,255,.16);color:#7fa9ff}
  .toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);opacity:0;
         background:#2a3140;border:1px solid var(--line);padding:10px 16px;border-radius:10px;font-size:14px;
         transition:.25s;pointer-events:none;max-width:88vw;z-index:99}
  .toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
  .toast.err{border-color:var(--bad);color:#ffd6d6}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block;margin-right:6px}
  .dot.on{background:var(--ok)}
</style>
</head>
<body>
<header>
  <h1>📺 TV投屏 · 手机遥控</h1>
  <div class="ip"><span class="dot" id="dot"></span><span id="state">连接中…</span></div>
</header>

<main>
  <section class="card">
    <h2>① 选择电视 <button class="ghost" id="btnScan" style="padding:5px 10px;font-size:13px">扫描</button></h2>
    <div id="devices"><div class="muted">点击右上角「扫描」查找局域网电视</div></div>
  </section>

  <section class="card">
    <h2>② 选择片源</h2>
    <input type="search" id="q" placeholder="搜索片名…（留空则浏览目录）" autocomplete="off">
    <div class="crumb" id="crumb" style="margin-top:10px"></div>
    <div class="list" id="files"></div>
  </section>

  <section class="card">
    <h2>③ 遥控
      <span style="font-weight:400;font-size:12px;color:var(--dim)">
        <input type="checkbox" id="autoTc" checked style="vertical-align:-2px"> 兼容模式
      </span>
    </h2>
    <div class="now" id="nowPlaying">尚未投屏</div>
    <div class="bar"><i id="progbar"></i></div>
    <div class="times"><span id="tPos">--:--</span><span id="tDur">--:--</span></div>
    <input type="range" id="seek" min="0" max="100" value="0" step="1">
    <div class="ctl">
      <button id="btnPlay">▶ 播放</button>
      <button id="btnPause">⏸ 暂停</button>
      <button id="btnStop">⏹ 停止</button>
    </div>
    <div class="volrow">
      <span>🔊</span>
      <input type="range" id="vol" min="0" max="100" value="30">
      <span id="volTxt">30</span>
    </div>

    <div id="fmtBox" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)">
      <div class="muted" id="fmtText">未选择片源</div>
      <div class="bar" style="margin-top:8px"><i id="tcBar"></i></div>
      <div class="times"><span id="tcStat">未开始转码</span><span id="tcPct"></span></div>
      <button id="btnTc" style="margin-top:10px;width:100%">⚡ 转码成 MP4（转完可拖进度）</button>
    </div>
  </section>
</main>

<div class="toast" id="toast"></div>

<script>
(function(){
  const $ = s => document.querySelector(s);
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let curDir = "", selectedDevice = null, duration = 0, dragging = false,
      volTimer = null, seekTimer = null, currentRel = null,
      badgeCache = {}, badgeTimer = null, seekMode = 'native';

  function toast(msg, isErr){
    const t = $('#toast'); t.textContent = msg; t.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(t._h); t._h = setTimeout(()=>t.className='toast', 2600);
  }
  async function api(path, opts){
    const r = await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
    if(!r.ok) throw new Error('HTTP '+r.status);
    return r.json();
  }
  function fmt(s){
    s = Math.max(0, Math.floor(s||0));
    const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60;
    const p = n => String(n).padStart(2,'0');
    return h ? h+':'+p(m)+':'+p(x) : p(m)+':'+p(x);
  }

  // ---------- 设备 ----------
  function renderDevices(list){
    const box = $('#devices');
    if(!list.length){ box.innerHTML = '<div class="muted">未发现设备。请确认电视与本机在同一 Wi-Fi，且电视端已打开「投屏/DLNA」开关。</div>'; return; }
    box.innerHTML = list.map(d =>
      '<div class="dev'+(d.selected?' on':'')+'" data-key="'+esc(d.key)+'">'+
        '<span class="nm">'+esc(d.name)+'</span>'+
        '<span class="tag">'+(d.kind==='chromecast'?'Cast':'DLNA')+'</span>'+
      '</div>').join('');
    box.querySelectorAll('.dev').forEach(el=>{
      el.onclick = async ()=>{
        const key = el.dataset.key;
        el.parentNode.querySelectorAll('.dev').forEach(x=>x.classList.remove('on'));
        el.classList.add('on');
        selectedDevice = key;
        try { await api('/api/select', {method:'POST', body: JSON.stringify({key})}); toast('已选择：'+el.querySelector('.nm').textContent); }
        catch(e){ toast('选择失败 '+e.message, true); }
      };
    });
  }

  $('#btnScan').onclick = async function(){
    this.disabled = true; this.textContent = '扫描中…';
    $('#devices').innerHTML = '<div class="muted">正在扫描 DLNA / Chromecast…</div>';
    try{
      const r = await api('/api/scan', {method:'POST'});
      renderDevices(r.devices || []);
      toast('发现 ' + (r.devices||[]).length + ' 台设备');
    }catch(e){ toast('扫描失败 '+e.message, true); $('#devices').innerHTML='<div class="muted">扫描失败</div>'; }
    this.disabled = false; this.textContent = '扫描';
  };

  // ---------- 片源 ----------
  function renderFiles(data){
    curDir = data.dir || '';
    const crumb = $('#crumb');
    if(curDir){
      const parts = curDir.split('/');
      let acc = '', html = '<a data-dir="">🏠 根目录</a>';
      parts.forEach(p=>{ acc = acc ? acc+'/'+p : p; html += ' / <a data-dir="'+acc+'">'+p+'</a>'; });
      crumb.innerHTML = html;
      crumb.querySelectorAll('a').forEach(a=>{ a.onclick = ()=> loadDir(a.dataset.dir); });
    } else { crumb.innerHTML = ''; }

    let html = (data.dirs||[]).map(d =>
      '<div class="item" data-dir="'+esc(d.rel)+'"><span class="nm">📁 '+esc(d.name)+'</span><span class="sz">›</span></div>').join('');
    html += (data.files||[]).map(f =>
      '<div class="item" data-file="'+esc(f.rel)+'" data-name="'+esc(f.name)+'">'+
        '<span class="nm">🎬 '+esc(f.name)+'</span>'+
        '<span class="badge" data-b="1"></span>'+
        '<span class="sz">'+f.size_text+'</span></div>').join('');
    if(!html) html = '<div class="muted" style="padding:8px">这里没有媒体文件</div>';
    $('#files').innerHTML = html;

    $('#files').querySelectorAll('.item').forEach(el=>{
      el.onclick = ()=>{
        if(el.dataset.dir !== undefined && el.dataset.dir !== '') return loadDir(el.dataset.dir);
        if(el.dataset.file) return doCast(el.dataset.file, el.dataset.name);
      };
    });
    applyBadges();
    scheduleBadges();
  }

  // ---------- 格式徽标 ----------
  function applyBadges(){
    document.querySelectorAll('#files .item[data-file]').forEach(el=>{
      const span = el.querySelector('.badge');
      if(!span) return;
      const b = badgeCache[el.dataset.file];
      if(!b){ span.textContent = ''; span.className = 'badge'; return; }
      if(b.cached){ span.textContent = '缓存✓'; span.className = 'badge ck'; }
      else if(b.direct){ span.textContent = '直连'; span.className = 'badge ok'; }
      else { span.textContent = '需转码'; span.className = 'badge tc'; }
    });
  }
  function scheduleBadges(){
    clearTimeout(badgeTimer);
    badgeTimer = setTimeout(()=>loadBadges(curDir), 250);
  }
  async function loadBadges(dir){
    try{
      const r = await api('/api/analyze_dir?dir='+encodeURIComponent(dir||''));
      Object.assign(badgeCache, r.results || {});
      applyBadges();
      if(r.pending) badgeTimer = setTimeout(()=>loadBadges(dir), 1200);
    }catch(e){ /* 静默 */ }
  }
  async function loadDir(dir){
    $('#q').value = '';
    try{ renderFiles(await api('/api/browse?dir='+encodeURIComponent(dir||''))); }
    catch(e){ toast('读取目录失败 '+e.message, true); }
  }
  let qTimer = null;
  $('#q').oninput = function(){
    clearTimeout(qTimer);
    const kw = this.value.trim();
    qTimer = setTimeout(async ()=>{
      if(!kw) return loadDir(curDir);
      try{
        $('#crumb').innerHTML = '<span>搜索：'+kw+'</span>';
        renderFiles({dir:'', dirs:[], files: await api('/api/search?q='+encodeURIComponent(kw))});
      }catch(e){ toast('搜索失败 '+e.message, true); }
    }, 300);
  };

  async function doCast(rel, name){
    const dev = selectedDevice;
    toast('正在推送：' + (name || rel));
    try{
      const r = await api('/api/cast', {method:'POST', body: JSON.stringify({rel, device_key: dev})});
      if(r.ok){
        const mt = r.mode === 'transcode' ? '（实时转码 · 可拖进度 · 边播边缓存中）'
                 : r.mode === 'cache' ? '（使用转码缓存·可拖进度）' : '';
        toast('✅ 已推送到 ' + r.device + ' ' + mt);
      }
      else { toast('❌ ' + r.error, true); }
    }catch(e){ toast('投屏失败 '+e.message, true); }
    setTimeout(refreshState, 800);
    setTimeout(pollTranscode, 500);
  }

  // ---------- 遥控 ----------
  async function act(action, value){
    try{
      const r = await api('/api/action', {method:'POST', body: JSON.stringify({action, value})});
      if(!r.ok) toast(r.error, true);
    }catch(e){ toast(e.message, true); }
  }
  $('#btnPlay').onclick  = ()=>act('play');
  $('#btnPause').onclick = ()=>act('pause');
  $('#btnStop').onclick  = ()=>act('stop');

  $('#vol').oninput = function(){ $('#volTxt').textContent = this.value; };
  $('#vol').onchange = function(){
    clearTimeout(volTimer);
    const v = parseInt(this.value,10);
    volTimer = setTimeout(()=>act('volume', v), 150);
  };
  $('#seek').addEventListener('pointerdown', ()=>{ dragging = true; });
  $('#seek').addEventListener('input', function(){
    dragging = true; $('#tPos').textContent = fmt(this.value);
  });
  $('#seek').addEventListener('change', function(){
    const v = parseInt(this.value,10);
    clearTimeout(seekTimer);
    seekTimer = setTimeout(()=>doSeek(v), 180);
  });
  async function doSeek(v){
    if(seekMode === 'restream') toast('正在从 ' + fmt(v) + ' 重新起流…');
    try{
      const r = await api('/api/action', {method:'POST', body: JSON.stringify({action:'seek', value:v})});
      if(!r.ok) toast('定位失败：' + r.error, true);
      else if(seekMode === 'restream') toast('✅ 已定位到 ' + fmt(v));
    }catch(e){ toast('定位失败 '+e.message, true); }
    dragging = false;
    setTimeout(refreshState, 400);
  }

  // ---------- 转码 ----------
  $('#btnTc').onclick = async function(){
    if(!currentRel){ toast('请先选一个片源', true); return; }
    this.disabled = true;
    try{
      const r = await api('/api/transcode', {method:'POST', body: JSON.stringify({rel: currentRel})});
      if(!r.ok){ toast(r.error, true); this.disabled = false; return; }
      toast('开始转码：' + r.src + '（可以边等边用手机做别的）');
      $('#tcStat').textContent = '转码中…';
      pollTranscode();
    }catch(e){ toast(e.message, true); this.disabled = false; }
  };
  async function pollTranscode(){
    const btn = $('#btnTc');
    try{
      const st = await api('/api/transcode/status');
      const pct = st.percent || 0;
      $('#tcBar').style.width = pct + '%';
      $('#tcPct').textContent = (st.state === 'running' || st.state === 'done') ? pct + '%' : '';
      if(st.state === 'running'){
        btn.disabled = true;
        $('#tcStat').textContent = st.eta ? ('转码中 · 剩余约 ' + fmt(st.eta)) : '转码中…';
        setTimeout(pollTranscode, 1200);
      } else if(st.state === 'done'){
        btn.disabled = false;
        $('#tcStat').textContent = '✅ 转码完成，重新投屏即可拖进度';
        toast('转码完成');
        loadBadges(curDir);
      } else if(st.state === 'error'){
        btn.disabled = false;
        $('#tcStat').textContent = '❌ 转码失败';
        toast('转码失败：' + (st.error || ''), true);
      } else {
        btn.disabled = false;
        $('#tcStat').textContent = '未开始转码';
        $('#tcBar').style.width = '0%';
      }
    }catch(e){ btn.disabled = false; }
  }
  $('#autoTc').onchange = async function(){
    const on = this.checked;
    try{
      await api('/api/config', {method:'POST', body: JSON.stringify({auto_transcode: on})});
      toast(on ? '兼容模式已开：不支持的格式自动转码' : '兼容模式已关：只推原始文件');
    }catch(e){ toast(e.message, true); }
  };

  // ---------- 状态轮询 ----------
  async function refreshState(){
    try{
      const s = await api('/api/state');
      $('#dot').className = 'dot' + (s.device_online ? ' on' : '');
      $('#state').textContent = s.device ? s.device : '未选择电视';
      if(s.device_key) selectedDevice = selectedDevice || s.device_key;

      $('#nowPlaying').textContent = s.file ? ('🎬 ' + s.file) : '尚未投屏';
      duration = s.duration || 0;
      if(!dragging){
        $('#progbar').style.width = duration ? (100*s.position/duration).toFixed(1)+'%' : '0%';
        $('#seek').max = duration || 100;
        $('#seek').value = Math.min(s.position||0, duration||100);
        $('#tPos').textContent = fmt(s.position);
        $('#tDur').textContent = duration ? fmt(duration) : '--:--';
      }
      if(document.activeElement !== $('#vol')){
        $('#vol').value = s.volume; $('#volTxt').textContent = s.volume;
      }
      // 转码 / 格式
      const live = (s.play_mode === 'transcode');
      seekMode = s.seek_mode || 'native';
      // 转码流靠"服务端重推"定位，进度条同样可以拖
      $('#seek').disabled = !s.file;
      if(document.activeElement !== $('#autoTc')) $('#autoTc').checked = !!s.auto_transcode;
      if(s.file_rel){
        currentRel = s.file_rel;
        const b = badgeCache[s.file_rel];
        let t = '当前片源：' + (b ? b.summary : '检测中…');
        if(s.play_mode === 'transcode') t += ' · 实时转码（可拖进度 · 拖动后重新起流约 1 秒）';
        else if(s.play_mode === 'cache') t += ' · 转码缓存（可拖进度）';
        else if(s.play_mode === 'direct') t += ' · 直连播放';
        const cf = s.cache_finalize || {};
        if(cf.running) t += ' · 正在封装缓存';
        else if(cf.last && cf.last.state === 'done') t += ' · 缓存已就绪，下次秒开可拖';
        else if(cf.last && cf.last.state === 'error') t += ' · 缓存封装失败';
        if(!s.ffmpeg_ok) t += ' · ⚠ 未找到 ffmpeg';
        $('#fmtText').textContent = t;
      } else {
        $('#fmtText').textContent = '未选择片源';
      }
    }catch(e){
      $('#dot').className = 'dot';
      $('#state').textContent = '连接中断';
    }
  }

  loadDir('');
  refreshState();
  pollTranscode();
  setInterval(refreshState, 2000);
  setInterval(()=>{ if(!selectedDevice) refreshState(); }, 6000);
})();
</script>
</body>
</html>
"""
