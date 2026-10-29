# 员工形象素材

像素人物来自 BotHarness/BotPixel 的 @botharness/pixel-avatar **0.10.2**（MIT），使用官方 pixelFigure 的 body/head 层输出透明 SVG，未引入工具状态或 Agent 能力。对应预设、版本检查和生成方式见 tools/e2e/generate_employee_avatar_assets.mjs；许可见 LICENSE-BotPixel.txt。形象 ID 固定，修改既有外观应使用新 ID。机器人沿用用户提供的 V5 素材。

唯一形象目录为 nexus_api/employee_avatars.json；所有页面均读取保存的 avatar ID。
