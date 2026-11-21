"""Release identity and the start-up notice.

联系方式和授权说明都在这里，改这一处，弹窗、设置里的「关于」和使用说明会一起变。
"""

APP_VERSION = "0.2.2"
AUTHOR = "酪蛋白"
# 填上仓库地址后，说明里的 GitHub 会变成可点击链接；留空时只显示文字。
GITHUB_URL = "https://github.com/shanigeyu/VAT"

CONTACTS = (
    ("合作微信", "bcybqxz"),
    ("反馈交流 1 群", "1134667721"),
)

NOTE_LICENCE = "本软件由酪蛋白制作并上传，当前版本免费使用，禁止卖出与修改后流出，已申请软件著作权。完整条款见 LICENSE。"
NOTE_FEEDBACK = "希望大家使用中提出宝贵建议分享，我会进行软件的改进，后续更新会发布在 GitHub 并在 Q 群通知。"
NOTE_CONSULT = "另外，Codex、Claude 等相关问题也可咨询。"

# 弹窗里的正文，按顺序分段显示
NOTICE = (NOTE_LICENCE, NOTE_FEEDBACK, NOTE_CONSULT)


def version():
    return "v" + APP_VERSION
