# 通用知识

本目录保存经过验证、可以跨项目复用的工程知识，按工程主题、语言、框架和工具分类。

每条知识使用独立 Markdown 文件，只解决一个主要问题。语言或框架名称使用目录表示，例如 Java 知识存放在 `languages/java/`，具体知识使用可检索的短横线文件名，例如 `virtual-thread-pinning.md`。主题目录中的 `README.md` 只说明范围和提供导航，不承载具体知识结论。

## 分类索引

- [通用工程知识](engineering/README.md)：架构、编码风格、设计模式和代码评审等跨语言知识。
- [编程语言知识](languages/README.md)：Java、Python 和 TypeScript 等语言知识。
- [框架与平台知识](frameworks/README.md)：Spring、React 和 Docker 等框架与平台知识。
- [工程工具知识](tools/README.md)：Git、IDE 和 Linux 等工具知识。

新增正式知识时应使用 `system/templates/knowledge-entry.md`，填写统一元数据和必需章节，并同步更新所在主题目录的 `README.md` 与根目录 `CATALOG.md`。不得为了填充目录而创建未经验证的示例知识。

上述分类不是封闭清单。存在至少一条已经验证的真实知识、现有分类无法合理容纳且新分类边界清晰时，可以按照 `system/rules/CONTRIBUTING.md` 主动创建新分类；不得强行归类或预先创建空目录。
