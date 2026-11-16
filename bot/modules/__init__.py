"""Feature modules.

CONTRACT - every module in this package must expose:
    COMMANDS: list[tuple[str, str]]   # (command_without_slash, short description) for /help + menu
    def register(app: telegram.ext.Application) -> None   # adds its handlers

Rules:
  * Wrap EVERY handler (commands, callbacks, message handlers) with bot.security.owner_only.
  * Use bot.utils.run_blocking for any blocking call; bot.utils.reply_text for text output.
  * Callback-query data must be prefixed with the module name, e.g. "power:shutdown",
    and CallbackQueryHandler must use pattern=r"^<module>:" so modules never collide.
    ("confirm:" is reserved for bot.confirm.)
  * Handlers do NOT re-implement logic: they parse args and call bot.actions (run_action /
    bot.confirm.confirm_and_run, which confirms dangerous actions), then bot.utils.send_result.
"""
MODULE_NAMES = ["system", "power", "shell", "files", "desktop", "media", "assistant", "automation", "ops"]
