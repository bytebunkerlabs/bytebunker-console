const { chromium } = require("playwright-core");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto((process.env.BB_UI_BASE || "http://127.0.0.1:18797") + "/");
  await page.waitForTimeout(1500);
  const leaks = new Set();
  for (const s of ["playground", "sessions", "agents", "jobs", "gateways", "models", "recipes", "cluster", "settings", "usage", "audit", "skills", "plugins", "mcp"]) {
    // grouped screens (Models, Tools) are reached through their group's tab
    const grp = { models: "models", recipes: "models", gateways: "models", mcp: "tools", skills: "tools", plugins: "tools" }[s];
    if (grp) await page.click(`[data-group="${grp}"]`).catch(() => {});
    await page.click(`[data-nav="${s}"]`).catch(() => {});
    await page.waitForTimeout(400);
    const found = await page.evaluate(() => [...document.querySelectorAll("[hidden]")]
      .filter((el) => getComputedStyle(el).display !== "none")
      .map((el) => el.tagName.toLowerCase() + (el.id ? "#" + el.id : "") + (el.className && typeof el.className === "string" ? "." + el.className.split(" ")[0] : "")));
    found.forEach((f) => leaks.add(s + ": " + f));
  }
  console.log([...leaks].join("\n") || "ok    no hidden element is visible");
  await browser.close();
  process.exit(leaks.size ? 1 : 0);
})();
