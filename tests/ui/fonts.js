const { chromium } = require("playwright-core");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const outside = [];
  page.on("request", (r) => { const u = new URL(r.url()); if (!["127.0.0.1", "localhost"].includes(u.hostname)) outside.push(r.url()); });
  await page.goto((process.env.BB_UI_BASE || "http://127.0.0.1:18797") + "/");
  await page.waitForTimeout(1500);
  const r = await page.evaluate(async () => {
    await document.fonts.ready;
    return { sans: document.fonts.check("14px Geist"), mono: document.fonts.check("14px 'Geist Mono'"),
             loaded: [...document.fonts].filter((f) => f.status === "loaded").map((f) => f.family),
             body: getComputedStyle(document.body).fontFamily.split(",")[0] };
  });
  const ok = r.sans && r.mono && r.body === "Geist" && !outside.length;
  console.log((ok ? "ok    " : "FAIL  ") + "fonts from the app, nothing from outside" + (ok ? "" : "   -> " + JSON.stringify(r) + " outside: " + outside.join(", ")));
  await browser.close();
  process.exit(ok ? 0 : 1);
})();
