import csv
import discord
from discord.ext import commands

class Sheet:
    """乗り番管理クラス"""
    PART_LIST = ("Vn1st", "Vn2nd", "Va", "Vc", "Cb", "Fl", "Ob", "Cl", "Fg", "Hr", "Tp", "Tb", "Tuba", "Perc")
    PROGRAM_LIST = ("前", "中", "メイン")
    FILENAME_LIST = [program + "_sheet.csv" for program in PROGRAM_LIST]

    def __init__(self, program):
        if(program not in self.PROGRAM_LIST):
            raise ValueError('Sheet.__init__ error: program が見つかりません')
        self.program = program
        self.filename = self.FILENAME_LIST[self.PROGRAM_LIST.index(program)]
        self.sheet_dict = {}
        self.already_added_pult_list = []

    def append(self, part, pult: int, member: discord.Member):
        if(part not in self.PART_LIST):
            raise ValueError('Sheet.append Error: partが見つかりません')
        if (part, pult) in self.already_added_pult_list:
            raise ValueError(f'Sheet.append Error: (part, pult) = ({part}, {pult}) は既に存在します')
        if member.id in self.sheet_dict:
            raise ValueError(f'Sheet.append Error: {member} は既に存在します')
        if member.nick is not None:
            self.sheet_dict[member.id] = (part, pult, member.nick)
        else:
            self.sheet_dict[member.id] = (part, pult, member.global_name)
        self.already_added_pult_list.append((part, pult))
        
    def load_csv(self):
        with open(self.filename) as sheet:
            reader = csv.reader(sheet)
            for row in reader:
                if(row[0] not in self.PART_LIST):
                    continue
                    #raise ValueError(f'Sheet.load_csv Error: part {row[0]} が見つかりません')
                self.sheet_dict[int(row[2])] = (row[0], int(row[1]), row[3])
                self.already_added_pult_list.append((row[0], int(row[1])))
                
    def save_csv(self):
        file = open(self.filename, 'w')
        for member_id in self.sheet_dict:
            file.write(self.sheet_dict[member_id][0]) # part
            file.write(',')
            file.write(str(self.sheet_dict[member_id][1])) #pult
            file.write(',')
            file.write(str(member_id)) # discord id
            file.write(',')
            file.write(self.sheet_dict[member_id][2]) #name
            file.write('\n')
        file.close()

    def clear(self):
        self.sheet_dict.clear()
        self.already_added_pult_list.clear()
        
    def delete(self, member: discord.Member):
        self.already_added_pult_list.remove((self.sheet_dict[member.id][0], self.sheet_dict[member.id][1]))
        del self.sheet_dict[member.id]
        
    

if __name__ == '__main__':
    mae_sheet = Sheet("前")
    mae_sheet.load_csv()
    print(mae_sheet.sheet_dict)
    mae_sheet.save_csv(ctx)
