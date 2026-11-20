/* Сквозная проверка функций беты в браузере: заявка, регистрация по коду, обратная связь, экспорт данных, смена e-mail, скрытие кнопок по ролям.
   Запуск: BASE_URL=http://localhost:8000 ADMIN_TOKEN=... node e2e/beta_site.js
   Сервер: DEMO_MODE=true BETA_INVITE_ONLY=true (код подтверждения показывается на экране), заданный ADMIN_TOKEN, загруженные демо-изменения. */
const { chromium } = require('playwright');
const BASE = process.env.BASE_URL || 'http://localhost:8000', ADMIN = process.env.ADMIN_TOKEN;
let fails = 0;
const step = async (n, f) => { try { const r = await f(); console.log('OK  ', n, r ? '\n     ' + r : ''); } catch (e) { fails++; console.log('FAIL', n, '\n     ' + e.message); } };
const admin = (pg, path, opt = {}) => pg.evaluate(async ([p, o, t]) => {
  const r = await fetch('/api/admin' + p, { ...o, headers: { 'X-Admin-Token': t, 'Content-Type': 'application/json' }, body: o.body ? JSON.stringify(o.body) : undefined });
  return r.json();
}, [path, opt, ADMIN]);
(async () => {
  const b = await chromium.launch(); const ctx = await b.newContext({ acceptDownloads: true, viewport: { width: 1280, height: 900 } }); const pg = await ctx.newPage();
  const errs = []; pg.on('pageerror', e => errs.push(e.message));
  await pg.goto(BASE + '/'); await pg.waitForSelector('.hq-hero');
  await step('заявка на бету: проверка полей и отправка', async () => {
    await pg.goto(BASE + '/#/beta'); await pg.waitForSelector('#betaForm');
    await pg.click('#betaForm button[type=submit]'); await pg.waitForSelector('[aria-invalid="true"]'); await pg.waitForTimeout(400);   // форма перерисована, фокус поставлен
    await pg.fill('#b-company', 'ООО Ромашка'); await pg.fill('#b-person', 'Иван'); await pg.fill('#b-email', 'ivan@example.org'); await pg.check('#betaForm input[name=consent]');
    await pg.click('#betaForm button[type=submit]'); await pg.waitForSelector('text=Заявка отправлена');
    const d = await admin(pg, '/applications'); const a = d.applications.find(x => x.email === 'ivan@example.org'); if (!a || a.company !== 'ООО Ромашка' || a.person !== 'Иван') throw new Error(JSON.stringify(d));
    await pg.click('.modal [data-i="0"]');
  });
  await step('регистрация без кода отклоняется, с кодом проходит', async () => {
    await pg.goto(BASE + '/#/register'); await pg.waitForSelector('#r-invite');
    await pg.fill('#r-name', 'Анна Ковалёва'); await pg.fill('#r-mail', 'anna@example.org'); await pg.fill('#r-pass', 'Passw0rd!x'); await pg.check('#regForm input[name=agree]');
    await pg.click('#regForm button[type=submit]'); await pg.waitForSelector('#r-invite[aria-invalid="true"]'); await pg.waitForTimeout(400);
    const code = (await admin(pg, '/invites', { method: 'POST', body: { count: 1 } })).codes[0];
    await pg.fill('#r-invite', code); await pg.fill('#r-pass', 'Passw0rd!x'); await pg.click('#regForm button[type=submit]'); await pg.waitForSelector('text=Подтвердите', { timeout: 8000 }).catch(() => { });
    const me = await pg.evaluate(async () => (await fetch('/api/auth/me')).json()); if (!me.user || me.user.email !== 'anna@example.org') throw new Error('не вошли: ' + JSON.stringify(me));
  });
  await step('компания и тариф через API, карточка открывается', async () => {
    await pg.evaluate(async () => {
      const j = (m, u, b) => fetch('/api' + u, { method: m, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }).then(r => r.json());
      const me = await (await fetch('/api/auth/me')).json();
      const reg = await fetch('/api/auth/resend-code', { method: 'POST', headers: { 'X-Requested-With': 'x' } }).then(r => r.json());
      await j('POST', '/auth/verify-email', { code: reg.dev_code });
      await j('POST', '/company', { form: 'ООО', name: 'Ромашка' });
    });
    const c = await pg.evaluate(async () => (await fetch('/api/company')).json());
    await pg.evaluate(async ([id, t]) => fetch('/api/admin/companies/' + id + '/plan', { method: 'PUT', headers: { 'X-Admin-Token': t, 'Content-Type': 'application/json' }, body: JSON.stringify({ plan: 'pro' }) }), [c.id, ADMIN]);
    await pg.goto(BASE + '/#/app'); await pg.reload(); await pg.waitForSelector('.app-shell, .sidebar, main', { timeout: 8000 });
  });
  let cid;
  await step('обратная связь на карточке: «полезно» и оценка', async () => {
    cid = await pg.evaluate(() => CHANGES[0].id);
    await pg.goto(BASE + '/#/app/change/' + cid); await pg.waitForSelector('[data-act=fbuse]');
    await pg.click('[data-act=fbuse]'); await pg.waitForSelector('[data-act=fbuse][aria-pressed=true]');
    await pg.click('[data-act=fbrate][data-n="4"]'); await pg.waitForSelector('[data-act=fbrate][data-n="4"][aria-pressed=true]');
    const f = await pg.evaluate(async id => (await fetch('/api/changes/' + id + '/feedback')).json(), cid);
    if (!f.useful || f.rating !== 4) throw new Error(JSON.stringify(f));
    await pg.click('[data-act=fbnr]'); await pg.waitForSelector('[data-act=fbnr][aria-pressed=true]');
    await pg.waitForSelector('[data-act=fbuse][aria-pressed=false]');
    await pg.reload(); await pg.waitForSelector('[data-act=fbnr][aria-pressed=true]');     // оценка пережила перезагрузку
    const rep = await admin(pg, '/feedback'); return JSON.stringify({ total: rep.total, avg: rep.avg_rating, nr: rep.not_relevant_share });
  });
  await step('метрики видят события', async () => {
    const m = await admin(pg, '/metrics?weeks=1'); const w = m.weeks[0];
    if (!(w.signups >= 1 && w.change_opens >= 1 && w.feedback_count >= 1 && w.active_users >= 1)) throw new Error(JSON.stringify(w));
    return JSON.stringify(w);
  });
  await step('аккаунт: выгрузка данных', async () => {
    await pg.goto(BASE + '/#/app/account'); await pg.waitForSelector('[data-act=exportdata]');
    const [dl] = await Promise.all([pg.waitForEvent('download'), pg.click('[data-act=exportdata]')]);
    const txt = require('fs').readFileSync(await dl.path(), 'utf8'); const j = JSON.parse(txt);
    if (j.user.email !== 'anna@example.org' || !j.consents.length || !j.feedback.length) throw new Error(txt.slice(0, 300));
    return dl.suggestedFilename();
  });
  await step('аккаунт: смена e-mail с кодом', async () => {
    await pg.click('[data-act=chemail]'); await pg.waitForSelector('#ce-m');
    await pg.fill('#ce-m', 'anna.new@example.org'); await pg.fill('#ce-p', 'Passw0rd!x'); await pg.click('.modal [data-i="1"]');
    await pg.waitForSelector('#ce-c'); const hint = await pg.textContent('.modal .m-body');
    const code = (hint.match(/Демо-код:\s*(\d{6})/) || [])[1]; if (!code) throw new Error('нет демо-кода: ' + hint);
    await pg.fill('#ce-c', code); await pg.click('.modal [data-i="1"]');
    await pg.waitForFunction(() => S.user.email === 'anna.new@example.org', null, { timeout: 5000 });
    const me = await pg.evaluate(async () => (await fetch('/api/auth/me')).json()); if (me.user.email !== 'anna.new@example.org') throw new Error(JSON.stringify(me));
  });
  await step('роли: у наблюдателя нет кнопок управления', async () => {
    await pg.goto(BASE + '/#/app/team'); await pg.waitForSelector('table');
    const r = await pg.evaluate(() => { const was = S.role; S.role = 'viewer'; const out = { invite: viewTeam().includes('data-act="invite"'), roleSelect: viewTeam().includes('data-role='), save: viewProfile().includes('step-actions" hidden'), canPlan: can('owner'), member: can('member') }; S.role = 'owner'; out.ownerInvite = viewTeam().includes('data-act="invite"'); out.ownerSelect = viewTeam().includes('data-act="invite"'); S.role = was; return out; });
    if (r.invite || r.roleSelect || !r.save || r.canPlan || r.member || !r.ownerInvite) throw new Error(JSON.stringify(r));
  });
  await step('мобильная версия страницы беты', async () => {
    const m = await b.newContext({ viewport: { width: 375, height: 800 } }); const p = await m.newPage(); await p.goto(BASE + '/#/beta'); await p.waitForSelector('#betaForm');
    const over = await p.evaluate(() => document.documentElement.scrollWidth - innerWidth); if (over > 2) throw new Error('горизонтальная прокрутка ' + over);
    await p.screenshot({ path: process.env.SHOT || '/tmp/beta_mobile.png', fullPage: true }); await m.close();
  });
  console.log('\nОшибки браузера:', errs.length ? errs.join(' | ') : 'нет'); await b.close(); process.exit(fails || errs.length ? 1 : 0);
})();
