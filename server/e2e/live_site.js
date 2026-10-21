/* Сквозная проверка сайта вместе с сервером (Playwright). Сервер должен работать в режиме показа:
     DEMO_MODE=true TELEGRAM_WEBHOOK_SECRET=hooksecret python -m app.cli seed && uvicorn app.main:create_app --factory
   Запуск:  BASE_URL=http://localhost:8000 node e2e/live_site.js   (нужны node и playwright) */
const { chromium } = require('playwright');const {execFileSync}=require('child_process');const cache={};
const BASE=process.env.BASE_URL||'http://localhost:8000';
const log=(...a)=>console.log(...a);
(async()=>{
const b=await chromium.launch();const ctx=await b.newContext({viewport:{width:1360,height:860}});const pg=await ctx.newPage();
const errs=[];pg.on('pageerror',e=>errs.push('PAGEERROR '+e.message));pg.on('console',m=>{if(m.type()==='error'&&!/fonts|ERR_|Failed to load resource/.test(m.text()))errs.push('CONSOLE '+m.text())});
pg.on('response',r=>{if(r.url().includes('/api/')&&r.status()>=500)errs.push('HTTP '+r.status()+' '+r.url())});
await pg.route(/fonts\.(googleapis|gstatic)\.com/,r=>{const u=r.request().url();try{if(!cache[u])cache[u]=execFileSync('curl',['-sS','-m','30','-A','Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36',u],{maxBuffer:50e6});r.fulfill({body:cache[u],contentType:u.includes('googleapis')?'text/css':'font/woff2',headers:{'access-control-allow-origin':'*'}})}catch(e){r.abort()}});
const shot=async n=>{await pg.waitForTimeout(500);await pg.screenshot({path:n+'.png'})};
const step=async(name,fn)=>{try{await fn();log('OK  ',name)}catch(e){log('FAIL',name,'—',String(e.message).split('\n')[0]);await shot('fail_'+name.replace(/\W+/g,'_'))}};
const hash=()=>pg.evaluate(()=>location.hash);
const email='e2e'+Date.now()%100000+'@example.org';

await step('сайт открылся в рабочем режиме',async()=>{await pg.goto(BASE+'/');await pg.waitForSelector('.hq-hero');if(!await pg.evaluate(()=>LIVE))throw new Error('LIVE=false')});
await step('регистрация',async()=>{
  await pg.goto(BASE+'/#/register');await pg.waitForSelector('#regForm');
  await pg.fill('#r-name','Анна Тестова');await pg.fill('#r-mail',email);await pg.fill('#r-pass','Passw0rd1');await pg.check('input[name=agree]');
  await pg.click('#regForm button[type=submit]');await pg.waitForSelector('#obVerify');});
await step('неверный код отклоняется',async()=>{
  for(let i=0;i<6;i++)await pg.fill('#code-'+i,'0');
  await pg.click('#obVerify button[type=submit]');await pg.waitForTimeout(600);
  const t=await pg.textContent('#obVerify');if(!/Неверный код/.test(t))throw new Error('нет сообщения: '+t.slice(0,120));});
await step('код подтверждения (режим показа)',async()=>{
  const code=await pg.evaluate(()=>OB.devCode);if(!/^\d{6}$/.test(code))throw new Error('нет dev_code: '+code);
  await shot('01_verify');
  for(let i=0;i<6;i++)await pg.fill('#code-'+i,code[i]);
  await pg.click('#obVerify button[type=submit]');await pg.waitForSelector('#obCompany');});
await step('создание компании',async()=>{
  await pg.fill('#ob-name','Тест Логистика');await pg.click('#obCompany button[type=submit]');await pg.waitForSelector('#obBasics');});
await step('отрасль и размер',async()=>{
  await pg.selectOption('#ob-ind','Транспорт и логистика');await pg.check('input[name=size][value=small]');
  await pg.click('#obBasics button[type=submit]');await pg.waitForSelector('#obTopics');});
await step('сферы мониторинга: лимит бесплатного тарифа',async()=>{
  await pg.click('[data-obtopic=pd]');await pg.click('[data-obtopic=tax]');await pg.click('#obTopics button[type=submit]');
  await pg.waitForSelector('.toast, [role=status]',{timeout:3000}).catch(()=>{});
  if((await hash())!=='#/onboarding')throw new Error('должен остаться на шаге, hash='+await hash());
  await shot('02_limit');
  await pg.click('[data-obtopic=tax]');await pg.click('#obTopics button[type=submit]');await pg.waitForFunction(()=>location.hash==='#/app',null,{timeout:8000});});
await step('кабинет открылся',async()=>{await pg.waitForSelector('.topbar');const t=await pg.textContent('.topbar h1');if(!/Анна/.test(t))throw new Error(t);await shot('03_overview_free');});
await step('на бесплатном тарифе профиль не персонализирует',async()=>{
  const n=await pg.evaluate(()=>CHANGES.filter(isPersonal).length);if(n!==0)throw new Error('personal='+n)});
await step('демо-панель: профиль демо-компанией',async()=>{
  await pg.click('[data-act=demo]');await pg.click('[data-demo=fill]');await pg.waitForFunction(()=>profilePct()>=70,null,{timeout:15000});
  await pg.waitForTimeout(600);await shot('04_overview_filled');
  const info=await pg.evaluate(()=>({plan:S.plan,personal:CHANGES.filter(isPersonal).map(c=>c.id).sort(),attn:attention().map(c=>c.id).sort(),pct:profilePct()}));
  log('     ',JSON.stringify(info));
  if(info.plan!=='pro')throw new Error('plan '+info.plan);if(info.personal.length!==8)throw new Error('personal '+info.personal.length);if(info.attn.length!==3)throw new Error('attn '+info.attn.length)});
await step('состояние сохраняется после перезагрузки',async()=>{
  await pg.reload();await pg.waitForSelector('.topbar');const pct=await pg.evaluate(()=>profilePct());if(pct<70)throw new Error('pct '+pct)});
await step('дайджесты и фильтр «только мои»',async()=>{
  await pg.goto(BASE+'/#/app/digests');await pg.waitForSelector('[data-filter=personal]');await pg.check('[data-filter=personal]');await pg.waitForTimeout(400);
  const n=await pg.evaluate(()=>document.querySelectorAll('.change-item').length);if(n<1)throw new Error('нет карточек');await shot('05_digests');});
await step('карточка изменения с причинами и планом',async()=>{
  await pg.goto(BASE+'/#/app/change/consent');await pg.waitForSelector('article.detail');
  const t=await pg.textContent('article.detail');if(!/Согласие на обработку/.test(t)||!/Оформить согласие отдельно от оферты/.test(t))throw new Error('нет плана');await shot('06_change');});
await step('сохранить и отметить прочитанным',async()=>{
  await pg.click('[data-act=save]');await pg.click('[data-act=read]').catch(()=>{});await pg.waitForTimeout(500);
  await pg.reload();await pg.waitForSelector('article.detail');const s=await pg.evaluate(()=>S.saved.includes('consent'));if(!s)throw new Error('не сохранилось на сервере')});
await step('выполнить действие и убрать из «требует внимания»',async()=>{
  const before=await pg.evaluate(()=>attention().length);
  const cnt=await pg.locator('[data-actcheck]').count();for(let i=0;i<cnt;i++){await pg.locator('[data-actcheck]').nth(i).check();await pg.waitForTimeout(300)}
  await pg.reload();await pg.waitForSelector('article.detail');const after=await pg.evaluate(()=>attention().length);
  if(!(after<before))throw new Error(`before ${before} after ${after}`)});
await step('уведомления: сохранить настройки',async()=>{
  await pg.goto(BASE+'/#/app/notifications');await pg.waitForSelector('[data-n=emailFreq]');await pg.selectOption('[data-n=emailFreq]','weekly');await pg.click('[data-nimp=high]');
  await pg.click('[data-act=nsave]');await pg.waitForTimeout(600);await pg.reload();await pg.waitForSelector('[data-n=emailFreq]');
  const v=await pg.evaluate(()=>({f:S.notif.emailFreq,i:S.notif.minImpact}));if(v.f!=='weekly'||v.i!=='high')throw new Error(JSON.stringify(v));await shot('07_notifications');});
let tgcode='';
await step('Telegram: получить код',async()=>{
  await pg.goto(BASE+'/#/app/telegram');await pg.waitForSelector('[data-act=tgconnect]');await pg.click('[data-act=tgconnect]');await pg.waitForSelector('.code-box');
  tgcode=(await pg.textContent('.code-box')).trim();if(!/^LT-[A-Z0-9]{4}$/.test(tgcode))throw new Error(tgcode);await shot('08_tg_pending');});
await step('Telegram: бот присылает код → подключено',async()=>{
  const r=await fetch(BASE+'/api/telegram/webhook/'+(process.env.WEBHOOK_SECRET||'hooksecret')+'',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:{chat:{id:424242,type:'private'},from:{username:'anna_test'},text:tgcode}})});
  const j=await r.json();if(j.result!=='connected')throw new Error(JSON.stringify(j));
  await pg.click('[data-act=tgcheck]');await pg.waitForFunction(()=>S.tg.status==='connected');await shot('09_tg_connected');});
await step('команда: приглашение',async()=>{
  await pg.goto(BASE+'/#/app/team');await pg.waitForSelector('[data-act=invite]');await pg.click('[data-act=invite]');await pg.fill('#iv-m','kollega@example.org');
  await pg.click('.m-actions .btn-primary');await pg.waitForFunction(()=>S.team.some(m=>m.email==='kollega@example.org'),null,{timeout:6000});await shot('10_team');});
await step('команда: смена роли и удаление',async()=>{
  const sel=await pg.$('[data-role]');await sel.selectOption('viewer');await pg.waitForTimeout(500);
  const roles=await pg.evaluate(async()=>{const r=await fetch('/api/team');return (await r.json()).members.map(m=>m.role)});if(!roles.includes('viewer'))throw new Error(roles);
  await pg.click('[data-remove]');await pg.click('.m-actions .btn-danger');await pg.waitForTimeout(600);const n=await pg.evaluate(()=>S.team.length);if(n!==1)throw new Error('team '+n)});
await step('тариф: смена в режиме показа',async()=>{
  await pg.goto(BASE+'/#/app/billing');await pg.waitForSelector('[data-act=chooseplan]');
  await pg.evaluate(()=>ACT.chooseplan({dataset:{id:'basic'}}));await pg.click('.m-actions .btn-primary');await pg.waitForTimeout(800);
  if(await pg.evaluate(()=>S.plan)!=='pro')throw new Error('понижение с 4 сферами должно быть отклонено');   // на «Базовом» лимит 3 сферы
  await pg.evaluate(async()=>{await api('PUT','/company/profile/industry',{data:Object.assign(stepToApi('industry'),{topics:['pd','tax','labor']})})});
  await pg.evaluate(()=>ACT.chooseplan({dataset:{id:'basic'}}));await pg.click('.m-actions .btn-primary');
  await pg.waitForFunction(()=>S.plan==='basic',null,{timeout:6000});
  await pg.waitForFunction(()=>CH['consent']&&CH['consent']._locked,null,{timeout:6000}).catch(()=>{throw new Error('действия должны быть закрыты на «Базовом»')});
  await pg.goto(BASE+'/#/app/change/consent');await pg.waitForSelector('article.detail');const t=await pg.textContent('article.detail');if(!/на тарифе «Профессиональный»/.test(t))throw new Error('нет подсказки про тариф');await shot('11_basic');
  await pg.evaluate(()=>ACT.chooseplan({dataset:{id:'pro'}}));await pg.click('.m-actions .btn-primary');await pg.waitForFunction(()=>S.plan==='pro',null,{timeout:6000});});
await step('аккаунт: имя, пароль, сессии',async()=>{
  await pg.goto(BASE+'/#/app/account');await pg.waitForSelector('#accForm');await pg.waitForFunction(()=>sessions.length>=1&&sessions[0].d.includes('·'),null,{timeout:5000});
  await pg.fill('#a-name','Анна Т. Тестова');await pg.click('#accForm button[type=submit]');await pg.waitForTimeout(500);
  await pg.fill('#pw0','Passw0rd1');await pg.fill('#pw1','NewPassw0rd2');await pg.click('#pwForm button[type=submit]');await pg.waitForTimeout(600);
  const err=await pg.textContent('#pwErr');if(err.trim())throw new Error('pw: '+err);await shot('12_account');});
await step('поддержка',async()=>{
  await pg.goto(BASE+'/#/app/support');await pg.waitForSelector('#supForm');await pg.fill('#s-m','Проверка обращения в поддержку');await pg.click('#supForm button[type=submit]');await pg.waitForTimeout(600);
  const r=await pg.evaluate(async()=>(await (await fetch('/api/support')).json()).tickets.length);if(r!==1)throw new Error('tickets '+r)});
await step('выход и вход с новым паролем',async()=>{
  await pg.evaluate(()=>ACT.logout());await pg.waitForFunction(()=>!S.user);
  await pg.goto(BASE+'/#/login');await pg.waitForSelector('#loginForm');await pg.fill('#l-mail',email);await pg.fill('#l-pass','Passw0rd1');await pg.click('#loginForm button[type=submit]');await pg.waitForTimeout(600);
  const bad=await pg.textContent('#loginForm');if(!/Неверная почта или пароль/.test(bad))throw new Error('нет ошибки');
  await pg.fill('#l-pass','NewPassw0rd2');await pg.click('#loginForm button[type=submit]');await pg.waitForFunction(()=>location.hash==='#/app',null,{timeout:8000});await pg.waitForSelector('.topbar');
  const nm=await pg.evaluate(()=>S.user.name);if(nm!=='Анна Т. Тестова')throw new Error(nm)});
await step('закрытые страницы без входа перенаправляют',async()=>{
  const c2=await b.newContext();const p2=await c2.newPage();await p2.goto(BASE+'/#/app/digests');await p2.waitForTimeout(1500);const h=await p2.evaluate(()=>location.hash);if(h!=='#/login')throw new Error(h);await c2.close()});
await step('мобильная версия кабинета',async()=>{
  await pg.setViewportSize({width:390,height:820});await pg.goto(BASE+'/#/app');await pg.waitForSelector('.topbar');await shot('13_mobile');});
log('\nОшибки браузера:',errs.length?errs:'нет');
await b.close()})().catch(e=>{console.error('E2E CRASH',e);process.exit(1)});
