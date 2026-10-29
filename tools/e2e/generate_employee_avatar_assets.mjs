/** 从固定 BotPixel 包生成本地透明素材，不手绘或重写其渲染器。
 * npm install --prefix /tmp/nexus-botpixel @botharness/pixel-avatar@0.10.2 --ignore-scripts
 * BOTPIXEL_ROOT=/tmp/nexus-botpixel/node_modules/@botharness/pixel-avatar node tools/e2e/generate_employee_avatar_assets.mjs
 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
const root = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const packageRoot = process.env.BOTPIXEL_ROOT;
if (!packageRoot)
  throw new Error(
    "请通过 BOTPIXEL_ROOT 指向固定版本的 @botharness/pixel-avatar",
  );
const info = JSON.parse(
  await fs.readFile(path.join(packageRoot, "package.json"), "utf8"),
);
if (info.version !== "0.10.2")
  throw new Error("素材只允许从固定的 0.10.2 生成；升级需新增形象 ID");
const { AVATAR_PRESETS, pixelFigure } = await import(
  pathToFileURL(path.join(packageRoot, "dist/index.js")).href
);
const service = path.join(root, "services/nexus-api");
const directory = path.join(service, "web/public/avatars");
await fs.mkdir(directory, { recursive: true });
const robotNames = [
  "薄荷绿机器人",
  "蓝白机器人",
  "薰衣草机器人",
  "琥珀机器人",
  "青柠机器人",
  "珊瑚机器人",
];
const items = robotNames.map((label, index) => ({
  id: `robot-v1-${index}`,
  label,
  family: "robot",
  source: "Nexus V5",
  portrait: `/office/person${index}_down_0.png`,
  office: {
    frames: [
      `/office/person${index}_sit_0.png`,
      `/office/person${index}_sit_1.png`,
    ],
    width: 74,
    height: 104,
    top: 13,
  },
}));
const choices = [
  ["beret", "金发贝雷帽", 0],
  ["braids", "棕发花冠", 1],
  ["silver", "银发夹克", 6],
  ["headphones", "耳机眼镜", 9],
  ["tie", "短发领带", 11],
  ["hime", "黑发发饰", 4],
];
for (const [key, label, index] of choices) {
  const id = `botpixel-v1-${key}`,
    recipe = AVATAR_PRESETS[index];
  const figure = pixelFigure(recipe, 0);
  const svg = `<!-- BotPixel @botharness/pixel-avatar 0.10.2, MIT; see LICENSE-BotPixel.txt -->\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width="128" height="128" shape-rendering="crispEdges">${figure.body}${figure.head}</svg>\n`;
  await fs.writeFile(path.join(directory, `${id}.svg`), svg);
  const portrait = `/avatars/${id}.svg`;
  items.push({
    id,
    label,
    family: "pixel",
    source: "BotPixel 0.10.2",
    portrait,
    office: { frames: [portrait, portrait], width: 98, height: 98, top: 18 },
  });
}
await fs.copyFile(
  path.join(packageRoot, "LICENSE"),
  path.join(directory, "LICENSE-BotPixel.txt"),
);
await fs.writeFile(
  path.join(service, "nexus_api/employee_avatars.json"),
  JSON.stringify({ version: 1, defaultId: "robot-v1-0", items }, null, 2) +
    "\n",
);
await fs.writeFile(
  path.join(directory, "README.md"),
  "# 员工形象素材\n\n像素人物来自 BotHarness/BotPixel 的 @botharness/pixel-avatar **0.10.2**（MIT），使用官方 pixelFigure 的 body/head 层输出透明 SVG，未引入工具状态或 Agent 能力。对应预设、版本检查和生成方式见 tools/e2e/generate_employee_avatar_assets.mjs；许可见 LICENSE-BotPixel.txt。形象 ID 固定，修改既有外观应使用新 ID。机器人沿用用户提供的 V5 素材。\n\n唯一形象目录为 nexus_api/employee_avatars.json；所有页面均读取保存的 avatar ID。\n",
);
console.log(
  `generated ${choices.length} BotPixel figures; ${items.length} appearances in catalog`,
);
