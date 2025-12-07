import os
import json
import http.client
import sys
import webbrowser
import uuid
import re
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import PyPDF2
import docx

# --- 1. CẤU HÌNH BAN ĐẦU ---
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default_secret_key_change_me')

base_dir = os.path.dirname(os.path.abspath(__file__))
database_url = os.environ.get('DATABASE_URL')

if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    print("--> USING CLOUD DATABASE")
else:
    db_path = os.path.join(base_dir, 'instance', 'story_project.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
    print("--> USING LOCAL SQLITE")

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

UPLOAD_FOLDER = os.path.join(base_dir, 'static', 'uploads')
if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)

instance_folder = os.path.join(base_dir, 'instance')
if not os.path.exists(instance_folder): os.makedirs(instance_folder)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# --- 2. MODELS ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    is_locked = db.Column(db.Boolean, default=False)
    stories = db.relationship('Story', backref='author', lazy=True)

class Style(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    content = db.Column(db.Text, nullable=False)

class Story(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    prompt_data = db.Column(db.Text, nullable=True)
    comics = db.relationship('Comic', backref='story', lazy=True)

class Comic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.Integer, db.ForeignKey('story.id'), nullable=False)
    panels_content = db.Column(db.Text, nullable=False)

class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.String(50), default=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    user = db.relationship('User', backref=db.backref('feedbacks', lazy=True))

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- 3. HELPER FUNCTIONS ---
def configure_ai():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key: return None
    return api_key

def extract_text_from_file(file):
    text = ""
    filename = file.filename.lower()
    try:
        if filename.endswith('.pdf'):
            reader = PyPDF2.PdfReader(file)
            for page in reader.pages: text += page.extract_text() + "\n"
        elif filename.endswith('.docx'):
            doc = docx.Document(file)
            for para in doc.paragraphs: text += para.text + "\n"
        else: return None
    except: return None
    return text

def generate_story_ai(api_key, prompt):
    try:
        conn = http.client.HTTPSConnection("api.yescale.io")
        payload = json.dumps({
            "model": "gemini-2.5-pro-thinking",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.8 
        })
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        conn.request("POST", "/v1/chat/completions", payload, headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8")
        if res.status != 200: return f"ERROR: {res.status}"
        
        response_json = json.loads(data)
        if 'choices' in response_json:
             content = response_json['choices'][0]['message']['content']
             return content.replace('**', '')
        return "Error parsing response"
    except Exception as e: return f"System Error: {e}"

def robust_json_extract(text):
    """
    Cố gắng trích xuất JSON từ phản hồi của AI, xử lý cả trường hợp
    có Markdown code blocks (```json ... ```) hoặc text thường.
    """
    try:
        # 1. Thử tìm khối code Markdown trước (chính xác nhất)
        code_block_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
        match = re.search(code_block_pattern, text, re.DOTALL)
        
        json_str = ""
        if match:
            json_str = match.group(1)
        else:
            # 2. Nếu không có markdown, tìm cặp ngoặc nhọn {} bao quanh nội dung lớn nhất
            # Tìm dấu { đầu tiên
            start_idx = text.find('{')
            # Tìm dấu } cuối cùng
            end_idx = text.rfind('}')
            
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                json_str = text[start_idx : end_idx + 1]
            else:
                return None # Không tìm thấy cấu trúc JSON

        # 3. Làm sạch các lỗi cú pháp phổ biến của AI
        # Xóa comments kiểu // (nếu có)
        json_str = re.sub(r'//.*', '', json_str)
        # Fix lỗi dấu phẩy thừa cuối danh sách/object (ví dụ: {"a": 1,} -> {"a": 1})
        json_str = re.sub(r',\s*([\]}])', r'\1', json_str)
        
        return json.loads(json_str)
        
    except Exception as e:
        print(f"JSON Parsing Error: {e}")
        return None
    
# --- 4. ADVANCED PROMPT ENGINEERING (UPDATED) ---

CEFR_LEVEL_GUIDELINES = {
    "PRE A1": "Simple Present (be/have/action). Short sentences (3-6 words). Focus on visual actions.",
    "A1": "Present Simple/Continuous. Basic conjunctions (and, but). Dialogues are simple Q&A.",
    "A2": "Past Simple, Future (will/going to). Adverbs of frequency. Coordinated sentences.",
    "B1": "Narrative tenses (Past Continuous), Conditionals (1 & 2), Reasons (because/so). Expressing feelings/opinions.",
    "B2": "Passive voice, Reported speech, Relative clauses. Nuanced vocabulary and abstract ideas.",
    "C1": "Complex sentence structures, Inversion, Idiomatic expressions. Literary tone.",
    "C2": "Sophisticated style, Implicit meaning, Cultural references, Irony/Humor."
}

def create_prompt_for_ai(inputs):
    cefr_level = inputs['level'].upper()
    vocab_list_str = ", ".join(inputs['vocab'])
    
    # 1. Setting Context
    setting_val = inputs['setting'].strip()
    setting_instr = f"**SETTING:** {setting_val}" if setting_val else "**SETTING:** A realistic setting in Vietnam (e.g., Saigon street, Hanoi cafe, Da Lat). Atmosphere is key."

    # 2. Logic Structure (Book vs Page)
    if cefr_level in ["PRE A1", "A1", "A2"]:
        structure_instr = """
        **STRUCTURE: PICTURE BOOK (Visual Focus)**
        - Divide into **8-10 'PAGES'**. Label: `--- PAGE [X] ---`
        - Content per page: 2-3 sentences max. Clear action.
        """
        repetition_rule = "Repeat target words 3-4 times naturally."
    else:
        structure_instr = """
        **STRUCTURE: SHORT STORY (Narrative Focus)**
        - Divide into **3-5 CHAPTERS**. Label: `CHAPTER [X]: [Title]`
        - Focus on flow, paragraphing, and dialogue.
        """
        repetition_rule = "Weave target words into the story naturally (approx 3-5 times each)."

    # 3. Master Prompt - NATURAL FLOW & CONSISTENCY
    prompt = f"""
    **Role:** Best-selling Author of Graded Readers.
    **Goal:** Write a story that is engaging, emotional, and educational.
    
    **CORE INPUTS:**
    - Level: {cefr_level}
    - Length: ~{inputs['count']} words.
    - Theme: {inputs['theme']}
    - Main Character: {inputs.get('main_char', 'A relatable character')}
    
    **MANDATORY GUIDELINES:**
    
    1. **STRONG OPENING (Context):** - The story **MUST** start with a **Title** (format: Title).
       - The **First Paragraph** MUST clearly introduce the **Main Character** and the **Setting/Context** immediately.
    
    2. **STORYTELLING:** - **Show, Don't Tell:** Instead of saying "He was sad", describe his actions.
       - **Inner Monologue:** Show what the character is thinking/feeling.
       - **Dialogue:** Use natural conversation to advance the plot.
    
    3. **VOCABULARY INTEGRATION (Natural Flow):**
       - **Target Words:** [{vocab_list_str}]
       - {repetition_rule}
       - **IMPORTANT:** Do NOT bold, underline, or highlight the target words. Keep it looking like a real book.
    
    4. {setting_instr}
    
    5. {structure_instr}
    
    6. **GRAMMAR & TONE:** - Follow {CEFR_LEVEL_GUIDELINES.get(cefr_level, "Standard")} grammar rules.
       - Tone: Encouraging, Relatable, Human.

    **OUTPUT FORMAT:**

    # [Creative Title]

    [STORY CONTENT HERE]

    ---
    Graded Definitions ({cefr_level})
    *Provide clear definitions for the target vocabulary ({vocab_list_str}). IMPORTANT: The definition language must be suitable for {cefr_level} learners (simple and clear).*
    *Format:*
    -word: definition.
    """
    return prompt

def create_comic_script_prompt(story_content):
    return f"""
    **Role:** Cinematic Art Director.
    **Task:** Create 12 distinct visual descriptions for Single Movie Screenshots based on the story.
    **INPUT STORY:** {story_content}
    
    **VISUAL RULES:**
    - **Format:** Single full-screen image description. NO "comic panels", NO "split screens".
    - **Style:** "Disney/Pixar 3D style or 2D Vector Art (Ligne Claire), vibrant colors."
    - **Character Consistency:** "A cute 4-year-old Vietnamese boy named Nhân (RED T-SHIRT, BLUE SHORTS)."
    - **Safety:** Child is PHYSICALLY SAFE. Drama is in the environment (wind, rain).

    **OUTPUT JSON:**
    {{
      "panels": [ 
        {{ "panel_number": 1, "visual_description": "Cinematic wide shot...", "caption": "..." }} 
      ]
    }}
    """

def create_translation_prompt(inputs):
    cefr_level = inputs['level'].upper()
    return f"""
    **Role:** Expert Graded Translator & Poet (Folktale Specialist).
    **Task:** Retell the Vietnamese folktale "{inputs['folktale_name']}" in English.
    
    **CRITICAL INSTRUCTIONS:**
    1. **POETIC TRANSLATION:** Vietnamese folktales often have verses/rhymes. Identify them and translate them into **English Rhyming Couplets**.
    2. **GRADING:** - Level: {cefr_level}. Length: ~{inputs['count']} words.
    3. **OUTPUT:** Start with # English Title.
    """

# --- 5. ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        admin_pin = request.form.get('admin_pin')
        user = User.query.filter_by(username=username).first()
        
        if user and user.is_locked:
            flash('Locked.', 'danger'); return render_template('login.html')
        if user and user.username.lower() == 'admin' and admin_pin != "25121509":
            flash('Wrong PIN.', 'danger'); return render_template('login.html')
            
        if user and check_password_hash(user.password_hash, password):
            login_user(user); return redirect(url_for('index'))
        flash('Invalid login.', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        code = request.form.get('secret_code')
        if code not in ["BOSS_ONLY_999", "GV_VIP_2025"]: return redirect(url_for('register'))
        if username.lower() == 'admin' and code != "BOSS_ONLY_999": return redirect(url_for('register'))
        if User.query.filter_by(username=username).first(): return redirect(url_for('register'))
        
        db.session.add(User(username=username, password_hash=generate_password_hash(password)))
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template('index.html', all_styles=Style.query.all(), previous_inputs={}, user=current_user)

@app.route('/generate-story', methods=['POST'])
@login_required
def handle_generation():
    api_key = configure_ai()
    if not api_key: return jsonify({"story_result": "API Key Missing"}), 500
    data = request.form
    inputs = {
        "idea": data.get('idea'), "vocab": data.get('vocab_str', '').split(','),
        "level": data.get('cefr_level'), "count": data.get('word_count'), "theme": data.get('theme'),
        "main_char": data.get('main_char'), "setting": data.get('setting'),
        "style_samples": [], "negative_keywords": data.get('negative_keywords'),
        "target_audience": data.get('target_audience'), "num_support": data.get('num_support_char')
    }
    return jsonify({"story_result": generate_story_ai(api_key, create_prompt_for_ai(inputs))})

@app.route('/create-comic/<int:story_id>', methods=['POST'])
@login_required
def create_comic_direct(story_id):
    story = Story.query.get_or_404(story_id)
    api_key = configure_ai()
    
    try:
        # 1. Gọi AI để lấy kịch bản
        ai_response_text = generate_story_ai(api_key, create_comic_script_prompt(story.content))
        
        # 2. Sử dụng hàm trích xuất mạnh mẽ thay vì regex đơn giản
        data = robust_json_extract(ai_response_text)
        
        if not data:
            # Fallback: Nếu AI trả về lỗi hoặc text không parse được
            print("AI Response Raw:", ai_response_text) # Log để debug trên Render
            return jsonify({"error": "AI trả về dữ liệu không đúng định dạng JSON. Vui lòng thử lại."}), 500

        # Xử lý trường hợp AI trả về key khác (đôi khi AI dùng "scenes" thay vì "panels")
        panels_data = data.get('panels', data.get('scenes', data))
        
        # Đảm bảo panels_data là một list
        if not isinstance(panels_data, list):
             # Nếu AI trả về object đơn lẻ thay vì list, bọc nó lại
             if isinstance(panels_data, dict): panels_data = [panels_data]
             else: return jsonify({"error": "Cấu trúc JSON không hợp lệ (cần danh sách panels)."}), 500

        final_panels = []

        # 3. Xử lý Prompt (Giữ nguyên logic cũ của bạn)
        for panel in panels_data:
            # Linh hoạt lấy key: visual_description HOẶC description HOẶC prompt
            raw = panel.get('visual_description') or panel.get('description') or panel.get('prompt') or "A scene from the story"
            
            # Clean keywords
            for w in ["comic", "panel", "page", "grid", "speech bubble", "text"]: 
                raw = raw.replace(w, "image")
            
            final_prompt = f"A single cinematic movie still, full screen digital art. {raw} --ar 3:2 --no text speech bubbles comic grid collage"
            
            final_panels.append({
                "panel_number": panel.get('panel_number', len(final_panels) + 1),
                "image_url": "", 
                "prompt": final_prompt,
                "caption": panel.get('caption', '')
            })
            
        new_comic = Comic(story_id=story_id, panels_content=json.dumps(final_panels))
        db.session.add(new_comic)
        db.session.commit()
        return jsonify({"success": True, "redirect_url": url_for('view_comic', comic_id=new_comic.id)})
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/view-comic/<int:comic_id>')
@login_required
def view_comic(comic_id):
    comic = Comic.query.get_or_404(comic_id)
    return render_template('view_comic.html', panels=json.loads(comic.panels_content), title=comic.story.title, comic_id=comic.id, user=current_user)

@app.route('/get-batch-prompt/<int:comic_id>')
@login_required
def get_batch_prompt(comic_id):
    comic = Comic.query.get_or_404(comic_id)
    panels = json.loads(comic.panels_content)
    scenes = [p['prompt'].replace("A single cinematic movie still, full screen digital art.", "").replace("--ar 3:2 --no text speech bubbles comic grid collage", "").strip().replace(",", " ") for p in panels]
    batch = f"/imagine prompt: A single cinematic movie still, full screen digital art. {{ {', '.join(scenes)} }} --ar 3:2 --no text speech bubbles comic grid collage"
    return jsonify({"batch_prompt": batch})

@app.route('/upload-panel-image', methods=['POST'])
@login_required
def upload_panel_image():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    comic = Comic.query.get(request.form.get('comic_id'))
    fname = f"comic_{comic.id}_p{request.form.get('panel_number')}_{uuid.uuid4().hex[:6]}.png"
    file.save(os.path.join(UPLOAD_FOLDER, fname))
    
    panels = json.loads(comic.panels_content)
    for p in panels:
        if str(p['panel_number']) == str(request.form.get('panel_number')): p['image_url'] = f"/static/uploads/{fname}"
    comic.panels_content = json.dumps(panels)
    db.session.commit()
    return jsonify({"url": f"/static/uploads/{fname}"})

# --- ROUTE NÀY QUAN TRỌNG: FIX LỖI BUILDERROR ---
@app.route('/reuse-prompt/<int:story_id>')
@login_required
def reuse_prompt(story_id):
    story = Story.query.get_or_404(story_id)
    if story.user_id != current_user.id:
        flash("You do not have permission.", "danger")
        return redirect(url_for('saved_stories_page'))
    
    prev_inputs = {}
    if story.prompt_data:
        try:
            prev_inputs = json.loads(story.prompt_data)
        except:
            prev_inputs = {}
    return render_template('index.html', all_styles=Style.query.all(), previous_inputs=prev_inputs, user=current_user)
# -----------------------------------------------

# --- OTHER ROUTES ---
@app.route('/styles')
@login_required
def styles_page(): return render_template('manage_styles.html', styles=Style.query.all(), user=current_user)

@app.route('/add-style', methods=['POST'])
@login_required
def add_style():
    content = request.form.get('style_content', '')
    if request.files.get('style_file'): content = extract_text_from_file(request.files['style_file']) or content
    db.session.add(Style(name=request.form['style_name'], content=content))
    db.session.commit()
    return redirect(url_for('styles_page'))

@app.route('/delete-style', methods=['POST'])
@login_required
def delete_style():
    s = Style.query.filter_by(name=request.form['style_to_delete']).first()
    if s: db.session.delete(s); db.session.commit()
    return redirect(url_for('styles_page'))

@app.route('/saved-stories')
@login_required
def saved_stories_page(): return render_template('saved_stories.html', stories=Story.query.filter_by(user_id=current_user.id).order_by(Story.id.desc()).all(), user=current_user)

@app.route('/save-story', methods=['POST'])
@login_required
def handle_save_story():
    # Tự động lấy Title từ dòng đầu tiên (có dấu # hoặc không)
    content = request.form.get('story_content', '')
    title = content.strip().split('\n')[0].replace('#', '').strip() or "Untitled"
    
    db.session.add(Story(title=title, content=content, user_id=current_user.id, prompt_data=request.form.get('prompt_data_json')))
    db.session.commit()
    return redirect(url_for('saved_stories_page'))

@app.route('/delete-story', methods=['POST'])
@login_required
def handle_delete_story():
    s = Story.query.get(request.form.get('story_id'))
    if s and s.user_id == current_user.id: db.session.delete(s); db.session.commit()
    return redirect(url_for('saved_stories_page'))

@app.route('/edit-story/<int:story_id>', methods=['GET', 'POST'])
@login_required
def edit_story_page(story_id):
    s = Story.query.get_or_404(story_id)
    if s.user_id != current_user.id: return redirect(url_for('saved_stories_page'))
    if request.method == 'POST': s.title = request.form['title']; s.content = request.form['content']; db.session.commit(); return redirect(url_for('saved_stories_page'))
    return render_template('edit_story.html', story=s, user=current_user)

@app.route('/translate-story')
@login_required
def translate_page(): return render_template('translate_story.html', user=current_user)

@app.route('/handle-translation', methods=['POST'])
@login_required
def handle_translation():
    api_key = configure_ai()
    if not api_key: return jsonify({"story_result": "API Key Missing"}), 500
    
    data = request.form
    inputs = {
        "folktale_name": data.get('folktale_name'), 
        "level": data.get('cefr_level'), 
        "count": data.get('word_count')
    }
    return jsonify({"story_result": generate_story_ai(api_key, create_translation_prompt(inputs))})

@app.route('/add-quiz-to-saved', methods=['POST'])
@login_required
def add_quiz_to_saved():
    s = Story.query.get(request.form.get('story_id'))
    if s and s.user_id == current_user.id:
        s.content += f"\n\n--- QUIZ ---\n{generate_story_ai(configure_ai(), f'Create {request.form.get('quiz_type')} quiz for: {s.content}')}"
        db.session.commit()
    return redirect(url_for('saved_stories_page'))

@app.route('/send-feedback', methods=['POST'])
@login_required
def send_feedback():
    if request.form.get('message'): db.session.add(Feedback(user_id=current_user.id, message=request.form.get('message'))); db.session.commit()
    return redirect(request.referrer)

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.username != 'admin': return "Access Denied", 403
    return render_template('admin.html', users=User.query.all(), feedbacks=Feedback.query.order_by(Feedback.id.desc()).all())

@app.route('/admin/reset-pass/<int:user_id>', methods=['POST'])
@login_required
def admin_reset_pass(user_id):
    if current_user.username == 'admin': u = User.query.get(user_id); u.password_hash = generate_password_hash("123456"); db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle-lock/<int:user_id>', methods=['POST'])
@login_required
def admin_toggle_lock(user_id):
    if current_user.username == 'admin': u = User.query.get(user_id); u.is_locked = not u.is_locked; db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete/<int:user_id>', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    if current_user.username == 'admin': u = User.query.get(user_id); Story.query.filter_by(user_id=u.id).delete(); db.session.delete(u); db.session.commit()
    return redirect(url_for('admin_dashboard'))

with app.app_context(): db.create_all()

if __name__ == '__main__':
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true': webbrowser.open_new('http://127.0.0.1:5000/')
    app.run(debug=True, port=5000)