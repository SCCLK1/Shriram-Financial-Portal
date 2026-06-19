html = open('output/card_daily.html', encoding='utf-8').read()
lines = html.split('\n')
for i, line in enumerate(lines):
    if 'SMALL' in line:
        # Print 15 lines from here
        for j in range(i, min(i+15, len(lines))):
            print(lines[j].rstrip())
        break
