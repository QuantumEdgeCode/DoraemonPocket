from MyQR import myqr
from urllib.parse import quote  # 导入quote函数

# 要生成QR码的链接或文本
url = "http://192.168.1.104/upload/明天见"
# 对中文进行URL编码
encoded_url = quote(url, safe='/:?=&')
# 保存的文件名，可以修改为其他文件名
save_name = 'upload.png'

# 生成QR码
myqr.run(words=encoded_url, save_name=save_name)
