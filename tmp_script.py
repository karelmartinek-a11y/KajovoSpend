from pathlib import Path 
text = Path('src/kajovospend/ui/main_window.py').read_text(encoding='utf-8') 
start = text.index('    def refresh_ops(self):') 
end = text.index('    def refresh_suspicious', start) 
