// VAT supplies the input as JSON in a fresh local module for this individual action.
function readDouyin() {
  const host = location.hostname;
  if (host !== "douyin.com" && !host.endsWith(".douyin.com")) return {status:"请在当前任务窗口打开抖音作品或搜索页。", rows:[]};
  const shown = el => !!el?.getClientRects().length && getComputedStyle(el).visibility !== "hidden";
  const text = el => (el?.innerText || el?.getAttribute("aria-label") || "").trim();
  const content = text(document.body);
  const gate = /请完成下列验证|拖动滑块|请完成安全验证|访问验证/.test(content) ? "请在 Ego 窗口完成抖音验证。" :
    /登录后即可搜索|登录后继续|打开[\s\S]{0,24}抖音APP[\s\S]{0,40}扫一扫/.test(content) ? "请在 Ego 窗口完成抖音登录。" : "";
  if (gate) return {gate:true, status:gate, rows:[]};
  const number = /^\s*(\d[\d,]*(?:\.\d+)?\s*(?:万|亿|[wWkK])?)\s*$/;
  function metric(root, selector, label) {
    for (const el of root.querySelectorAll(selector)) {
      if (!shown(el)) continue;
      const val = text(el).replaceAll(label,"").replace(/[:：]/g,"").trim();
      if (number.test(val)) return val;
    }
    return "";
  }
  function author(root) {
    return [...root.querySelectorAll('a[href*="/user/"]')].find(el=>shown(el) && !el.href.includes("/user/self"))?.href || "";
  }
  function fanCount(root) {
    const marked=metric(root,'[data-e2e="user-info-fans"],[data-e2e="user-info-follower"]',"粉丝");
    if(marked) return marked;
    for (const el of root.querySelectorAll("span,div,strong")) {
      if (!shown(el) || el.children.length || text(el)!=="粉丝") continue;
      for(const value of [text(el.nextElementSibling),text(el.parentElement).replace("粉丝","").trim()]) {
        if(number.test(value)) return value;
      }
    }
    return "";
  }
  if (location.pathname.startsWith("/user/")) {
    const followers=fanCount(document);
    return {rows:[], followers, profile:location.href, status:followers?"已读取主页公开粉丝数。":"主页未显示可识别的粉丝数。"};
  }
  const idFrom = raw => {
    try { const u=new URL(raw,location.href); if (!/(^|\.)douyin\.com$/.test(u.hostname)) return "";
      return u.pathname.match(/^\/video\/(\d+)(?:\/|$)/)?.[1] || (u.searchParams.get("modal_id")||"").match(/^\d+$/)?.[0] || "";
    } catch { return ""; }
  };
  const likeSelector='[data-e2e="video-player-digg"],[data-e2e="detail-video-digg"],[data-e2e="search-card-like"],[data-e2e="like-count"],[aria-label*="点赞"]';
  function cardLikes(card) {
    const marked=metric(card,likeSelector,"点赞");
    if(marked) return marked;
    const cover=card.querySelector(".videoImage");
    if(!cover) return "";
    const counts=[...cover.querySelectorAll("span")].filter(n=>shown(n) && !n.children.length && number.test(text(n)) && n.parentElement.querySelector("svg"));
    return counts.length===1 ? text(counts[0]) : "";
  }
  function detailLikes(root) {
    const marked=metric(root,likeSelector,"点赞");
    if(marked) return marked;
    // The desktop detail layout places the like, comment, favorite and share
    // controls together directly below the work title, beside its publish date.
    let panel=root.querySelector('h1,[data-e2e="video-desc"]')?.parentElement;
    for(let level=0;panel && level<6;level++,panel=panel.parentElement) {
      if(!text(panel).includes("发布时间")) continue;
      for(const group of panel.querySelectorAll("div")) {
        const items=[...group.children];
        if(items.length===4 && items.every(n=>shown(n) && number.test(text(n)) && n.querySelector('svg') && n.querySelector('[tabindex],button'))) return text(items[0]);
      }
      break;
    }
    return "";
  }
  const rows=new Map(), activeId=idFrom(location.href);
  if (activeId) {
    const active=document.querySelector('[data-e2e="feed-active-video"]') || document.querySelector('[role="dialog"]') || document;
    const title=text(active.querySelector('[data-e2e="video-desc"],[data-e2e="video-info-desc"],h1')) || document.title.replace(/\s*[-_]\s*抖音.*$/,"");
    rows.set(activeId,{title:title || "抖音作品 "+activeId,url:"https://www.douyin.com/video/"+activeId,
      likes:detailLikes(active),followers:fanCount(active),author:author(active)});
  } else {
    for (const link of document.querySelectorAll('a[href*="/video/"],a[href*="modal_id="],[data-aweme-id]')) {
      if (!shown(link)) continue;
      const id=idFrom(link.href || "") || (link.getAttribute("data-aweme-id")||"").match(/^\d+$/)?.[0];
      if (!id || rows.has(id)) continue;
      const card=link.closest('li,article,[data-e2e="search-card-item"],[data-e2e="search-card"]') || link;
      const caption=text(card.querySelector(".videoImage")?.nextElementSibling).split("\n")[0];
      const title=text(card.querySelector('[data-e2e*="title"],h2,h3')) || link.getAttribute("title") || caption || text(link) || text(card);
      if (title) rows.set(id,{title:title.slice(0,220),url:"https://www.douyin.com/video/"+id,
                              likes:cardLikes(card),followers:"",author:author(card)});
      if (rows.size>=35) break;
    }
  }
  return {rows:[...rows.values()], status:rows.size?"已读取页面公开显示的作品，缺失指标可点「核对指标」。":"请在 Ego 点开一条作品，再点击「读取当前页」。"};
}

const options = VAT_INPUT;
const name = "VAT 抖音选题 " + options.project;
const spaces = await listTaskSpaces();
const entry = spaces.find(s=>s.id===options.space && s.name===name) || spaces.find(s=>s.name===name);
if (!entry && options.action==="capture") throw Error("请先点击「在 Ego 搜索」。");
const task = !entry ? await taskSpace(name) : entry.ownership==="agentDelegatedToUser" ?
  await takeOverTaskSpace(entry.id) : entry.ownership==="user" ? await claimTaskSpace(entry.id) : await taskSpace(entry.id);
async function navigate(page,url) {
  await page.goto(url,{timeout:30000});
  return readPage(page);
}
async function readPage(page) {
  try { await page.waitForFunction(()=>{
    if (!/(^|\.)douyin\.com$/.test(location.hostname)) return true;
    const body=document.body.innerText;
    if (/登录后即可搜索|登录后继续|请完成.*验证|拖动滑块|访问验证/.test(body)) return true;
    if (location.pathname.startsWith("/user/")) return /粉丝\s*[\d,.]+/.test(body);
    if (location.pathname.startsWith("/video/") || new URL(location.href).searchParams.has("modal_id"))
      return body.includes("发布时间") && body.includes("粉丝") || document.querySelector('[data-e2e="video-player-digg"]')?.innerText.trim();
    if (location.pathname.startsWith("/search/")) return !!document.querySelector('a[href*="/video/7"],[data-aweme-id]');
    return true;
  },undefined,{timeout:8500}); } catch {}
  return page.evaluate(readDouyin);
}
let result;
let detailPage = entry ? options.detailPage : null;
try {
  const tabs=await task.tabs(), active=tabs.find(t=>t.active) || tabs[0];
  const page=active.label ? task.page(active.label) : await task.adopt(active.page);
  if (options.action==="search") {
    result=await navigate(page,"https://www.douyin.com/search/"+encodeURIComponent(options.keyword)+"?type=video");
  } else if (options.action==="capture") {
    result=await readPage(page);
  } else {
    const initial=await page.evaluate(readDouyin);
    if (initial.gate) result=initial;
    else {
      const managed=await task.pages();
      const detail=managed.find(p=>p.label===detailPage) || await task.newPage();
      detailPage=detail.label;
      const checked=[], fanCounts=new Map();
      const batch=options.action==="batch" ? options.row : [options.row];
      for (const src of batch) {
        if (!/^https:\/\/www\.douyin\.com\/video\/\d+$/.test(src.url)) continue;
        const work=await navigate(detail,src.url);
        const visible=work.rows?.find(r=>r.url===src.url) || {};
        const row={...src,...Object.fromEntries(Object.entries(visible).filter(([,v])=>v!=="" && v!=null))};
        checked.push(row);
        if (work.gate) { result=work; break; }
        if (!row.followers && row.author && /^https:\/\/(www\.)?douyin\.com\/user\/(?!self(?:[/?]|$))/.test(row.author)) {
          if (!fanCounts.has(row.author)) {
            const profile=await navigate(detail,row.author);
            if (profile.gate) { result=profile; break; }
            fanCounts.set(row.author,profile.followers || "");
          }
          if (fanCounts.get(row.author)) row.followers=fanCounts.get(row.author);
        }
      }
      result={...(result || {status:"已核对 "+checked.length+" 条作品。未公开或无法识别的指标保留为待核对。"}), rows:checked};
    }
  }
} finally { await task.handOff(); }
const payload = JSON.stringify({...result,space:task.spaceId,detailPage});
try {
  const {writeFile} = await import("node:fs/promises");
  await writeFile(options.resultFile,payload,{encoding:"utf8",flag:"wx"});
} catch {
  console.log("VAT_RESULT "+payload);
}
