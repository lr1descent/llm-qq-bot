const { test, expect } = require("@playwright/test");

test("admin edits persist, routing is enforced, full history is readable, and mobile fits", async ({
  page,
}) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "创建管理员" })).toBeVisible();
  await page.getByLabel("管理员密码").fill("uitest88");
  await page.getByLabel("确认密码").fill("uitest88");
  await page.getByRole("button", { name: "创建并进入" }).click();
  await expect(
    page.getByRole("heading", { name: "会话记录", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("尚未配置 OpenAI API Key。", { exact: false }),
  ).toBeVisible();

  await page.getByRole("link", { name: "人格提示词" }).click();
  await page
    .getByLabel("系统提示词")
    .fill("你是一位简洁、友善的群聊伙伴。先理解上下文，再自然回应。");
  await page.getByRole("button", { name: "保存人格设定" }).click();
  await expect(page.locator("#save-state")).toHaveText("已保存");
  await page.reload();
  await expect(page.getByLabel("系统提示词")).toHaveValue(
    "你是一位简洁、友善的群聊伙伴。先理解上下文，再自然回应。",
  );
  await page.screenshot({
    path: "artifacts/persona-desktop.png",
    fullPage: true,
  });

  await page.getByRole("link", { name: "模型路由" }).click();
  await page
    .getByRole("radio", { name: "默认 gpt-6.1-sol", exact: true })
    .check();
  await page
    .getByRole("checkbox", { name: "允许 gpt-5.6-luna", exact: true })
    .uncheck();
  await page.getByRole("button", { name: "保存路由规则" }).click();
  await expect(page.locator("#save-state")).toHaveText("已保存");
  await page.reload();
  await expect(
    page.getByRole("radio", { name: "默认 gpt-6.1-sol", exact: true }),
  ).toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "允许 gpt-5.6-luna", exact: true }),
  ).not.toBeChecked();
  await page.screenshot({
    path: "artifacts/models-desktop.png",
    fullPage: true,
  });

  await page.getByRole("link", { name: "会话记录" }).click();
  await page.getByRole("button", { name: "＋ 测试会话", exact: true }).click();
  await page.getByLabel("会话名称").fill("浏览器验证会话");
  await page.getByRole("button", { name: "创建会话", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "浏览器验证会话" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "发送消息" })).toBeDisabled();
  const created = await page.evaluate(async () => {
    const auth = await (await fetch("/api/auth/status")).json();
    const list = await (await fetch("/api/conversations")).json();
    const id = list.items[0].id;
    const headers = {
      "Content-Type": "application/json",
      "X-CSRF-Token": auth.csrf,
    };
    const rejected = await fetch(`/api/conversations/${id}/messages`, {
      method: "POST",
      headers,
      body: JSON.stringify({ content: "禁止调用 Luna", model: "gpt-5.6-luna" }),
    });
    const accepted = await fetch(`/api/conversations/${id}/messages`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        content:
          "浏览器完整消息验证 <script>window.XSS_EXECUTED=true</script>\n第二行保留。",
        model: "gpt-6.1-sol",
      }),
    });
    return { rejected: rejected.status, accepted: accepted.status };
  });
  expect(created).toEqual({ rejected: 422, accepted: 202 });
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.locator(".message-text")).toContainText("第二行保留。");
  await expect(page.locator(".run-state")).toContainText("OPENAI_API_KEY");
  expect(await page.evaluate(() => window.XSS_EXECUTED)).toBeUndefined();
  await page.getByRole("button", { name: "查看调用记录" }).click();
  await expect(
    page.getByRole("heading", { name: "调用详情", exact: true }),
  ).toBeVisible();
  await expect(page.locator("#modal-body")).toContainText("gpt-6.1-sol");
  await expect(page.locator("#modal-body")).toContainText(
    "你是一位简洁、友善的群聊伙伴",
  );
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.screenshot({
    path: "artifacts/conversation-desktop.png",
    fullPage: true,
  });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "artifacts/conversation-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "退出", exact: true }).click();
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  expect(errors).toEqual([]);
});
