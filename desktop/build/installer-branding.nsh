; 安装向导的自定义入口 —— 由 electron-builder 的 nsis.include 引入。
;
; 只做一件事：补一页品牌欢迎页。
; electron-builder 的「向导模式」默认从「安装选项」直接开始，左侧那张
; 164×314 的品牌图（MUI_WELCOMEFINISHPAGE_BITMAP）就只在最后一页「完成」出现一次，
; 首屏反而是通用模板 —— 白瞎了。加上欢迎页后，品牌图是用户看到的**第一屏**，
; 同时「完成」页仍然复用同一张图，两头呼应。
;
; skipPageIfUpdated：自动更新（安装器带 /S 或 electron-updater 静默调用）时直接跳过这一页，
; 不给升级流程凭空多一次点击。这个宏由 common.nsh 提供，必须在 MUI_PAGE_WELCOME 之前调用，
; 它定义的 MUI_PAGE_CUSTOMFUNCTION_PRE 才会挂到下一页上。
;
; 注意：本文件只在 oneClick=false（向导模式）时生效；one-click 安装器不会走这里。

!macro customWelcomePage
  !insertmacro skipPageIfUpdated
  !insertmacro MUI_PAGE_WELCOME
!macroend

; 固定「仅当前用户」安装。
; 由 multiUserUi.nsh 在「安装范围」页的 PRE 里插入：把 $isForceCurrentInstall 置 1 后，
; 该页会设置 per-user 模式并 Abort（整页跳过）。这样不会出现「选所有用户 → UAC 二次启动
; → 安装器窗口重开、重看欢迎/许可页」的流程；安装目录留在当前用户可写的位置，
; electron-updater 的静默更新也才成立（见 electron-builder.yml 的 nsis 注释）。
!macro customInstallMode
  StrCpy $isForceCurrentInstall "1"
!macroend