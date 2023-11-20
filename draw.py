from PIL import Image, ImageDraw, ImageFont
from sheet import Sheet

class PlayerBoxDrawer:
    """
    乗り番描画用クラス
    
    Attributes
    ----------
    position_list : dict of {str: [int, int]}
        各楽器の奏者ボックス左上の座標を格納したlistのdict
    img : Image
        PIL.Image のオブジェクト 描画用
    draw : ImageDraw
        PIL.ImageDrawのオブジェクト 描画ツールセット
    """
    BLACK  = (  0,  0,  0)
    WHITE  = (255,255,255)
    GREEN  = (196,244,201)
    GRAY   = (220,220,220)
    YELLOW = (238,246,150)
    BLUE   = (163,230,242)
    RED    = (255,  0,  0)
    NAME_FONT = ImageFont.truetype('GenShinGothic-Medium.ttf', 24)
    PART_FONT = ImageFont.truetype('GenShinGothic-Medium.ttf', 20)
    POS_TYPE_LIST = ("対向配置")
    PLAYER_BOX_SIZE = (130, 70)
    CONDUCTOR_BOX_SIZE = (180,120)
        
    def __init__(self, pos_type):
        if pos_type not in self.POS_TYPE_LIST:
            raise ValueError("PlayerBoxDrawer.__init__ Error: pos_typeが見つかりません")
        self.position_list = {part:[] for part in Sheet.PART_LIST}

        self.img = Image.new("RGB",(1920, 1080),(255,255,255))
        self.draw = ImageDraw.Draw(self.img)
                
        #対向配置
        for num in range(1, 11):
            self.position_list["Vn1st"].append(self.vn1st_position(num))
            self.position_list["Vn2nd"].append(self.vn2nd_position(num))
            self.position_list["Va"].append(self.va_position(num))
            self.position_list["Vc"].append(self.vc_position(num))
        for num in range(1, 5):
            self.position_list["Cb"].append(self.cb_position(num))
            self.position_list["Hr"].append(self.hr_position(num))
            self.position_list["Perc"].append(self.perc_position(num))
        for num in range(1, 4):
            self.position_list["Fl"].append(self.fl_position(num))
            self.position_list["Ob"].append(self.ob_position(num))
            self.position_list["Cl"].append(self.cl_position(num))
            self.position_list["Fg"].append(self.fg_position(num))
            self.position_list["Tb"].append(self.tb_position(num))
            self.position_list["Tp"].append(self.tp_position(num))
        self.position_list["Tuba"].append(self.tb_position(4))
        self.draw_logo()
        self.draw_conductor()
        self.draw_legend()
        
        
    def vn1st_position(self, num):
        if(num%2 == 1):
            x = 1100 + (num-1)/2 * 160
            y = 50 + (num-1)/2 * 20
            return (x, y)
        else:
            x = 1100 + (num-2)/2 * 160 - 10
            y = 140 + (num-2)/2 * 20
            return (x, y)

    def vn2nd_position(self, num): # 対向配置
        if(num%2 == 1):
            x = 690 - (num-1)/2 * 160
            y = 50 + (num-1)/2 * 20
            return (x, y)
        else:
            x = 690 - (num-2)/2 * 160 + 10
            y = 140 + (num-2)/2 * 20
            return (x, y)
            
    def va_position(self, num): # 対向配置
        if(num%2 == 1):
            x = 720 - (num-1)/2 * 160
            y = 260 + (num-1)/2 * 30
            return (x, y)
        else:
            x = 800 - (num-2)/2 * 160
            y = 340 + (num-2)/2 * 30
            return (x, y)
                
    def vc_position(self, num): # 対向配置, Vaと対称
        va_p = self.va_position(num)
        x = 1920 - va_p[0] - 130
        y = self.va_position(num)[1]
        return (x, y)
    
    def cb_position(self, num): # 対向配置
        if(num%2 == 1):
            x = 1710 + (num-1)/2 * 70
            y = 430 + (num-1)/2 * 90
            return (x, y)
        else:
            x = 1560 + (num-1)/2 * 70
            y = 460 + (num-1)/2 * 90
            return (x, y)
                    
    def fl_position(self, num):
        x = 980 + (num-1) * 160
        y = 600
        return (x, y)

    def ob_position(self, num):
        x = 810 - (num-1) * 160
        y = 600
        return (x, y)
    
    def cl_position(self, num):
        x = 980 + (num - 1) * 160
        y = 700
        return (x, y)

    def fg_position(self, num):
        x = 810 - (num - 1) * 160
        y = 700
        return (x, y)
    
    def tp_position(self, num):
        x = 980 + (num-1) * 160
        y = 830
        return (x, y)

    def tb_position(self, num):
        x = 810 - (num-1) * 160
        y = 830
        return (x, y)

    def perc_position(self, num):
        x = 810 - (num-1) * 160
        y = 930
        return (x, y)

    def hr_position(self, num): # 対向配置 4パートまで
        if(num <= 2):
            x = 1450 + (num-1) * 160
            y = 700 - (num-1) * 20
            return (x, y)
        else:
            x = 1470 + (num-3) * 160
            y = 800 - (num-3) * 20
            return (x, y)

    def draw_playerbox(self, part, num:int, name, color, fontcolor):
        if part not in Sheet.PART_LIST:
            raise ValueError(f"{part}はパートリストにありません")
        if num == 0:
            raise ValueError("pult == 0 は使えません")
        upper_left_position = self.position_list[part][num-1]
        center_position = (upper_left_position[0] + self.PLAYER_BOX_SIZE[0]/2,
                           upper_left_position[1] + self.PLAYER_BOX_SIZE[1]/2)
        lower_right_position = (upper_left_position[0] + self.PLAYER_BOX_SIZE[0],
                                upper_left_position[1] + self.PLAYER_BOX_SIZE[1])

        name_position = (center_position[0],
                         center_position[1] + 15)
        part_position = (center_position[0],
                         center_position[1] - 15)
        self.draw.rectangle((upper_left_position, lower_right_position), fill=color, outline=self.BLACK, width=2)
        self.draw.text(name_position, name, fill=fontcolor, font=self.NAME_FONT, anchor="mm")
        self.draw.text(part_position, part, fill=fontcolor, font=self.PART_FONT, anchor="mm")
        
    def draw_legend(self):
        self.draw.rectangle((60,960,190,1030),fill=self.WHITE, outline=self.BLACK, width=2)
        self.draw.text((125, 995), "未回答", fill=self.RED, font=self.NAME_FONT, anchor="mm")
        self.draw.rectangle((60,890,190,960),fill=self.BLUE, outline=self.BLACK, width=2)
        self.draw.text((125, 925), "早退", fill=self.BLACK, font=self.NAME_FONT, anchor="mm")
        self.draw.rectangle((60,820,190,890),fill=self.YELLOW, outline=self.BLACK, width=2)
        self.draw.text((125, 855), "遅刻", fill=self.BLACK, font=self.NAME_FONT, anchor="mm")
        self.draw.rectangle((60,750,190,820),fill=self.GRAY, outline=self.BLACK, width=2)
        self.draw.text((125, 785), "欠席", fill=self.BLACK, font=self.NAME_FONT, anchor="mm")
        self.draw.rectangle((60,680,190,750),fill=self.GREEN, outline=self.BLACK, width=2)
        self.draw.text((125, 715), "出席", fill=self.BLACK, font=self.NAME_FONT, anchor="mm")

    def draw_conductor(self):
        box_size = self.CONDUCTOR_BOX_SIZE
        upper_left_position = (870, 50)
        center_position = (960, 110)
        lower_right_position = (1050,170)
        font = ImageFont.truetype('GenShinGothic-Medium.ttf', 50)
            
        self.draw.rectangle((upper_left_position, lower_right_position), fill=self.WHITE, outline=self.BLACK, width=3)
        self.draw.text(center_position, "cond.", fill=self.BLACK, font=font, anchor="mm")


    def draw_logo(self):
        logo = Image.open("logo.jpg")
        logo = logo.resize((logo.width // 12, logo.height // 12))
        self.img.paste(logo, (1400, 950))

    def draw_program(self, program):
        font = ImageFont.truetype('GenShinGothic-Medium.ttf', 30)
        position = (60,50)
        self.draw.text(position, program, fill=self.BLACK, font=font, anchor="mm")

    def save(self, filename):
        self.img.save(filename)
        
if __name__ == '__main__':
    boxdrawer = PlayerBoxDrawer("対向配置")
    img = Image.new("RGB", (1920, 1080), (256,256,256))
    draw = ImageDraw.Draw(img)
    boxdrawer.draw_playerbox(draw, "Vn2nd", 3, "Vn鈴木", boxdrawer.GREEN)
    boxdrawer.draw_legend(draw)
    img.save("image2.png")
    
    BLACK = (0,0,0)
    WHITE = (255,255,255)
    FONT = ImageFont.truetype('GenShinGothic-Medium.ttf', 25)
    def vn1st_position(num):
        if(num%2 == 1):
            x = 1100 + (num-1)/2 * 160
            y = 50 + (num-1)/2 * 20
            return (x, y)
        else:
            x = 1100 + (num-2)/2 * 160 - 10
            y = 140 + (num-2)/2 * 20
            return (x, y)

    def vn2nd_position(num): # 対向配置
        if(num%2 == 1):
            x = 690 - (num-1)/2 * 160
            y = 50 + (num-1)/2 * 20
            return (x, y)
        else:
            x = 690 - (num-2)/2 * 160 + 10
            y = 140 + (num-2)/2 * 20
            return (x, y)
            
    def va_position(num): # 対向配置
        if(num%2 == 1):
            x = 720 - (num-1)/2 * 160
            y = 260 + (num-1)/2 * 30
            return (x, y)
        else:
            x = 800 - (num-2)/2 * 160
            y = 340 + (num-2)/2 * 30
            return (x, y)
                
    def vc_position(num): # 対向配置, Vaと対称
        va_p = va_position(num)
        x = 1920 - va_p[0] - 130
        y = va_position(num)[1]
        return (x, y)
    
    def cb_position(num): # 対向配置
        if(num%2 == 1):
            x = 1710 + (num-1)/2 * 70
            y = 430 + (num-1)/2 * 90
            return (x, y)
        else:
            x = 1560 + (num-1)/2 * 70
            y = 460 + (num-1)/2 * 90
            return (x, y)
                    
    def fl_position(num):
        x = 980 + (num-1) * 160
        y = 600
        return (x, y)

    def ob_position(num):
        x = 810 - (num-1) * 160
        y = 600
        return (x, y)
    
    def cl_position(num):
        x = 980 + (num - 1) * 160
        y = 700
        return (x, y)

    def fg_position(num):
        x = 810 - (num - 1) * 160
        y = 700
        return (x, y)
    
    def tp_position(num):
        x = 980 + (num-1) * 160
        y = 830
        return (x, y)

    def tb_position(num):
        x = 810 - (num-1) * 160
        y = 830
        return (x, y)

    def perc_position(num):
        x = 810 - (num-1) * 160
        y = 930
        return (x, y)

    def hr_position(num): # 対向配置 4パートまで
        if(num <= 2):
            x = 1450 + (num-1) * 160
            y = 700 - (num-1) * 20
            return (x, y)
        else:
            x = 1470 + (num-3) * 160
            y = 800 - (num-3) * 20
            return (x, y)

    def draw_logo(draw):
        logo = Image.open("logo.jpg")
        logo = logo.resize((logo.width // 12, logo.height // 12))
        img.paste(logo, (1400, 950))
    
    def draw_conductor(draw):
        box_size = (180, 120)
        upper_left_position = (870, 50)
        center_position = (960, 110)
        lower_right_position = (1050,170)
            
        draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=3)
        FONT = ImageFont.truetype('GenShinGothic-Medium.ttf', 50)
        draw.text(center_position, "cond.", fill=(0,0,0), font=FONT, anchor="mm")

    def draw_playerbox(draw, name, position):
        box_size = (130, 70)
        for num in range(1, 11):
            upper_left_position = vn1st_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 11):
            upper_left_position = vn2nd_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 9):
            upper_left_position = va_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 8):
            upper_left_position = vc_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 4):
            upper_left_position = fl_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 4):
            upper_left_position = ob_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 4):
            upper_left_position = cl_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 4):
            upper_left_position = fg_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 4):
            upper_left_position = tp_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 5):
            upper_left_position = tb_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 5):
            upper_left_position = hr_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 5):
            upper_left_position = cb_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
        for num in range(1, 5):
            upper_left_position = perc_position(num)
            center_position = (upper_left_position[0] + box_size[0]/2, upper_left_position[1] + box_size[1]/2)
            lower_right_position = (upper_left_position[0] + box_size[0], upper_left_position[1] + box_size[1])
            draw.rectangle((upper_left_position, lower_right_position), fill=WHITE, outline=BLACK, width=2)
            draw.text(center_position, name, fill=BLACK, font=FONT, anchor="mm")
                
    
    img = Image.new("RGB", (1920, 1080), (256, 256, 256))

    draw = ImageDraw.Draw(img)

    draw_conductor(draw)
    draw_playerbox(draw,"Vn鈴木",0)
    draw_logo(draw)
    img.save("image.png")
