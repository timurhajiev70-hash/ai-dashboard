// Install (outside this project, or in an existing Node tool environment):
//   npm install playwright
//   npx playwright install chromium
// Run from the project root against an already running server:
//   BASE_URL=http://127.0.0.1:8787 PASSWORD='test-password' node tests/smoke.js
// If the server has no password, omit PASSWORD. No test runner is required.
'use strict';

const { chromium, request } = require('playwright');

const BASE_URL = (process.env.BASE_URL || 'http://127.0.0.1:8787').replace(/\/+$/, '');
const PASSWORD = process.env.PASSWORD || '';
const passed = [];
const failed = [];

async function check(name, fn) {
  try {
    await fn();
    passed.push(name);
  } catch (error) {
    failed.push(`${name}: ${error.message}`);
  }
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function visible(locator, name) {
  await locator.waitFor({ state: 'visible' });
  assert(await locator.isVisible(), `${name} не видно`);
}

async function main() {
  let api;
  let browser;
  let page;
  const consoleErrors = [];
  let loginSucceeded = false;

  try {
    api = await request.newContext({ baseURL: BASE_URL, timeout: 10000 });
    let health;
    await check('GET /api/health: HTTP 200 и ok:true', async () => {
      const response = await api.get('/api/health');
      assert(response.status() === 200, `HTTP ${response.status()}`);
      health = await response.json();
      assert(health && health.ok === true, 'нет ok:true');
      assert(typeof health.authRequired === 'boolean', 'нет authRequired');
    });
    if (!health) return;

    if (health.authRequired) {
      await check('Неверный пароль: HTTP 401', async () => {
        assert(PASSWORD, 'сервер требует пароль; задайте PASSWORD');
        const response = await api.post('/api/login', {
          data: { password: `${PASSWORD}__wrong__` },
        });
        assert(response.status() === 401, `ожидался 401, получен ${response.status()}`);
      });
      if (!PASSWORD) return;
    }

    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    page = await context.newPage();
    page.setDefaultTimeout(10000);
    page.on('console', (message) => {
      if (message.type() !== 'error') return;
      const text = message.text();
      const isExpectedPreLogin401 = health.authRequired
        && !loginSucceeded
        && /Failed to load resource.*\b401\b/i.test(text);
      if (!isExpectedPreLogin401) consoleErrors.push(text);
    });
    page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error.message}`));

    await check('Открытие страницы и вход', async () => {
      const response = await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
      assert(response && response.status() === 200, `страница: HTTP ${response ? response.status() : 'нет ответа'}`);
      if (health.authRequired) {
        await visible(page.locator('#login-form'), 'форма входа');
        await page.locator('#login-password').fill(PASSWORD);
        await page.locator('#login-submit').click();
      }
      await visible(page.locator('#app'), 'приложение после входа');
      loginSucceeded = true;
    });
    if (!(await page.locator('#app').isVisible())) return;

    const tabs = [
      ['systems', 'Системы', '#view-systems h2'],
      ['matrix', 'Матрица', '#view-matrix h2'],
      ['recommend', 'Кому дать задачу', '#view-recommend h2'],
      ['settings', 'Настройки', '#view-settings h2'],
    ];
    for (const [id, label, heading] of tabs) {
      await check(`Вкладка «${label}»: содержимое видно`, async () => {
        await page.locator(`#tab-${id}`).click();
        const panel = page.locator(`#view-${id}`);
        await visible(panel, `вкладка «${label}»`);
        await visible(page.locator(heading), `заголовок «${label}»`);
        assert((await panel.innerText()).trim().length > 0, 'вкладка пуста');
        assert(await page.locator(`#tab-${id}`).getAttribute('aria-selected') === 'true', 'вкладка не выбрана');
      });
    }

    await check('«Системы»: есть хотя бы одна карточка', async () => {
      await page.locator('#tab-systems').click();
      assert(await page.locator('#view-systems .sys-card').count() > 0, 'карточек .sys-card нет');
    });

    await check('Подбор: результат и непустой промпт', async () => {
      await page.locator('#tab-recommend').click();
      await page.locator('#rec-task').fill('найти письмо от главврача');
      const responsePromise = page.waitForResponse((response) =>
        response.url().startsWith(`${BASE_URL}/api/recommend`) && response.request().method() === 'POST');
      await page.locator('#rec-submit').click();
      const response = await responsePromise;
      assert(response.status() === 200, `/api/recommend: HTTP ${response.status()}`);
      await visible(page.locator('#view-recommend .rec-result'), 'результат подбора');
      const prompt = page.locator('#view-recommend .rec-result #rec-prompt');
      await visible(prompt, 'поле подготовленного промпта');
      assert((await prompt.inputValue()).trim().length > 0, 'промпт пуст');
    });

    await check('«Настройки»: видна ссылка экспорта', async () => {
      await page.locator('#tab-settings').click();
      await visible(page.locator('#view-settings a[href="/api/export"][download="config.json"]'), 'ссылка экспорта');
    });

    await check('Нет горизонтальной прокрутки при ширине 360px', async () => {
      await page.setViewportSize({ width: 360, height: 800 });
      for (const [id, label] of tabs) {
        await page.locator(`#tab-${id}`).click();
        await visible(page.locator(`#view-${id}`), `мобильная вкладка «${label}»`);
        const widths = await page.evaluate(() => ({
          viewport: document.documentElement.clientWidth,
          document: document.documentElement.scrollWidth,
        }));
        assert(widths.document <= widths.viewport + 1,
          `«${label}»: document.scrollWidth=${widths.document}, clientWidth=${widths.viewport}`);
      }
    });

    await check('Нет console.error и необработанных ошибок страницы', async () => {
      assert(consoleErrors.length === 0, consoleErrors.join(' | '));
    });
  } catch (error) {
    failed.push(`Запуск теста: ${error.message}`);
  } finally {
    if (browser) await browser.close();
    if (api) await api.dispose();
    console.log('Пройдено:');
    for (const name of passed) console.log(`  ✓ ${name}`);
    console.log('Упало:');
    for (const name of failed) console.log(`  ✗ ${name}`);
    if (failed.length === 0) console.log('  —');
  }
}

main().catch((error) => {
  console.error(`✗ Не удалось вывести итог: ${error.stack || error.message}`);
  failed.push(`Не удалось вывести итог: ${error.message}`);
}).finally(() => {
  process.exitCode = failed.length ? 1 : 0;
});
