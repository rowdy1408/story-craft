import os
import json
import http.client
import sys
import webbrowser
import uuid
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# --- 1. CẤU HÌNH BAN ĐẦU ---
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'secret_key_123')

# Đường dẫn DB
base_dir = os.path.dirname(os.path.abspath(__file__))

# Kiểm tra xem có ổ đĩa gắn ngoài (/var/data) không? (Trên Render sẽ có)
if os.path.exists('/var/data'):
    db_path = '/var/data/story_project.db'
    print("--> USING RENDER PERSISTENT DISK")
else:
    # Nếu chạy trên máy tính thì vẫn lưu vào instance như cũ
    db_path = os.path.join(base_dir, 'instance', 'story_project.db')
    print("--> USING LOCAL INSTANCE FOLDER")

app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
# --------------------------------------------------

# Đường dẫn Upload
UPLOAD_FOLDER = os.path.join(base_dir, 'static', 'uploads')
if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
instance_folder = os.path.join(base_dir, 'instance')
if not os.path.exists(instance_folder): os.makedirs(instance_folder)

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login' # Chưa đăng nhập thì đá về trang login

# --- 2. MODELS ---
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    # Thêm cột này để quản lý Khóa/Mở khóa
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
    # Thêm user_id để biết truyện của ai
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    comics = db.relationship('Comic', backref='story', lazy=True)

class Comic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.Integer, db.ForeignKey('story.id'), nullable=False)
    panels_content = db.Column(db.Text, nullable=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- 3. HELPER & CONFIG ---
def configure_ai():
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_API_KEY is empty.")
        return None
    return api_key

# --- 4. GỌI AI (YESCALE WRAPPER) ---
def generate_story_ai(api_key, prompt):
    try:
        conn = http.client.HTTPSConnection("api.yescale.io")
        payload = json.dumps({
            "model": "gemini-2.5-pro-thinking", 
            "messages": [{"role": "user", "content": prompt}], 
            "temperature": 0.7 
        })
        headers = {'Accept': 'application/json', 'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        print("--- Sending prompt to Yescale API... ---")
        conn.request("POST", "/v1/chat/completions", payload, headers)
        res = conn.getresponse()
        data = res.read().decode("utf-8")
        
        if res.status != 200: 
            return f"ERROR: API call failed ({res.status}). {data}"
        
        response_json = json.loads(data)
        if 'choices' in response_json and len(response_json['choices']) > 0:
             content = response_json['choices'][0]['message']['content']
             return content.replace('**', '') 
        else:
             return f"ERROR: Invalid response structure. {data}"

    except Exception as e:
        return f"ERROR: System error: {e}"

# --- 5. PROMPTS CHUYÊN SÂU (Đã phục hồi đầy đủ) ---
CEFR_LEVEL_GUIDELINES = {
    "PRE A1": """- **Grammar:** Strict Pre-A1 (Present Simple, Be, Have, Imperatives). Avg 3-8 words/sentence. NO complex sentences.""",
    "A1": """- **Grammar:** Simple Present & Continuous, Can, Have got. Short dialogues. Avg 6-10 words/sentence.""",
    "A2": """- **Grammar:** Simple Past, Future, Comparatives, Modals (must, should). Avg 8-12 words/sentence.""",
    "B1": """- **Grammar:** Narrative tenses, Conditionals, Relative clauses. Show, don't tell.""",
    "B2": """- **Grammar:** Passive voice, Reported speech, Complex sentences.""",
    "C1": """- **Style:** Literary, symbolic, advanced connectors.""",
    "C2": """- **Style:** Sophisticated, implicit meanings, philosophical themes."""
}

def create_prompt_for_ai(inputs):
    vocab_list_str = ", ".join(inputs['vocab'])
    cefr_level = inputs['level'].upper()
    
    raw_audience = inputs.get('target_audience', 'Children')
    audience_type = "CHILDREN"
    if any(x in raw_audience.lower() for x in ['adult', 'business', 'office', 'student']):
        audience_type = "ADULT"

    raw_support = inputs.get('num_support', '').strip()
    support_instruction = ""
    if not raw_support or raw_support == '0':
        support_instruction = "Add 1-2 generic background characters if needed for realism."
    else:
        support_instruction = f"Include exactly **{raw_support} generic supporting characters**."

    setting_val = inputs['setting'].strip()
    setting_instruction = f"**SETTING LOCK:** Must be in **{setting_val}**." if setting_val else "Setting: Authentic context."

    structure_instruction = ""
    tone_instruction = ""

    if cefr_level in ["PRE A1", "A1"]:
        if audience_type == "ADULT":
            structure_instruction = "- **Structure:** Write **3-5 clear PARAGRAPHS**. NO 'Page' breaks."
            tone_instruction = "**Context:** Adult daily life."
        else:
            structure_instruction = "- **Structure:** Split into **8-12 short 'PAGES'**. Label `--- PAGE [X] ---`. 1-2 sentences/page."
            tone_instruction = "**Tone:** Visual, simple for kids."
    else:
        structure_instruction = "- **Structure:** Split into **CHAPTERS**."
        tone_instruction = f"**Tone:** Engaging for {raw_audience}."

    grammar_rule = CEFR_LEVEL_GUIDELINES.get(cefr_level, CEFR_LEVEL_GUIDELINES["B1"])
    
    style_instr = ""
    if inputs['style_samples']:
        style_instr = "## STYLE REFERENCE\nMimic tone:\n" + "\n".join([f"Sample {i+1}: {s}" for i, s in enumerate(inputs['style_samples'])])

    avoid_instr = f"10. **AVOID:** {inputs['negative_keywords']}" if inputs.get('negative_keywords') else ""

    prompt = f"""
    **Role:** Expert Graded Reader Author for **{raw_audience}**.
    **Task:** Write a story optimized for fluency.
    
    **INPUTS:**
    - Idea: {inputs['idea']}
    - Theme: {inputs['theme']}
    - Level: {cefr_level}
    - Vocab: {vocab_list_str}
    - Length: {inputs['count']} words.
    
    **RULES:**
    1. **IDENTITY:** Main Char is **{inputs.get('main_char', 'Create one')}**. KEEP THIS NAME.
    2. **SUPPORT:** {support_instruction}. Include simple dialogue.
    3. **SETTING:** {setting_instruction}
    4. **RECYCLING:** Use required words 3-5 times.
    5. **FORMAT ({audience_type}):**
       {structure_instruction}
       {tone_instruction}
    6. **GRAMMAR:** {grammar_rule}
    7. **NO HIGHLIGHTING:** Plain text only.

    {avoid_instr}
    {style_instr}

    **OUTPUT:**
    # [Title]
    [
    STORY CONTENT:
    ...
    ]
    ---
    ## Graded Definitions ({cefr_level})
    ...
    """
    return prompt

def create_translation_prompt(inputs):
    cefr_level = inputs['level'].upper()
    level_guidelines = CEFR_LEVEL_GUIDELINES.get(cefr_level, CEFR_LEVEL_GUIDELINES["B1"])
    prompt = f"""
    **Role:** Expert Graded Translator & Poet.
    **Task:** Retell the Vietnamese folktale "{inputs['folktale_name']}" in English.
    **CRITICAL INSTRUCTIONS:**
    1. **POETIC TRANSLATION:** Identify iconic rhymes/verses. Translate them into **English Rhyming Couplets** (AABB or ABAB).
    2. **CONSTRAINTS:** Level: {cefr_level}. Length: {inputs['count']} words. Grammar: {level_guidelines}.
    **OUTPUT:** # [English Title] ...
    """
    return prompt

def create_quiz_only_prompt(story_text, quiz_type):
    return f"""
    **Role:** Educational Content Creator.
    **Task:** Create a Reading Quiz ({quiz_type}) for the story below.
    **Story:** {story_text}
    **Output:** ## Reading Quiz ... ## Answer Key ...
    """

def create_comic_script_prompt(story_content):
    return f"""
    **Role:** Professional Comic Book Director.
    **Task:** Convert the story into a Comic Script JSON.
    **INPUT STORY:** {story_content}
    **CRITICAL:**
    1. One Panel per Page.
    2. Caption must match story text verbatim.
    3. Generate Back Cover metadata.
    **OUTPUT JSON FORMAT:**
    {{
      "panels": [ {{ "panel_number": 1, "visual_description": "...", "caption": "..." }} ],
      "back_cover": {{ "summary": "...", "theme": "...", "level": "..." }}
    }}
    """
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user = User.query.filter_by(username=username).first()
        
        # Kiểm tra xem có bị khóa không (Code Admin Pro)
        if user and user.is_locked:
            flash('This account has been LOCKED by Admin.', 'danger')
            return render_template('login.html')

        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('index'))
            
        flash('Invalid username or password', 'danger')
    return render_template('login.html')

# --- 6. ROUTES AUTH (LOGIN/REGISTER) ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        code = request.form.get('secret_code')

        # --- CẤU HÌNH 2 MÃ BÍ MẬT ---
        TEACHER_CODE = "GV_VIP_2025"       # Mã cho đồng nghiệp
        ADMIN_CODE = "BOSS_ONLY_999"       # Mã riêng cho bạn (Sửa lại nhé!)
        # ----------------------------

        # TRƯỜNG HỢP 1: Dùng mã Admin
        if code == ADMIN_CODE:
            # Cho phép tạo mọi tên, kể cả 'admin'
            pass 
            
        # TRƯỜNG HỢP 2: Dùng mã Giáo viên
        elif code == TEACHER_CODE:
            # Nếu dùng mã giáo viên mà đòi đặt tên 'admin' -> CHẶN
            if username.lower() == 'admin':
                flash("This code cannot create Admin account!", "danger")
                return redirect(url_for('register'))
        
        # TRƯỜNG HỢP 3: Mã sai bét
        else:
            flash('Wrong Registration Code!', 'danger')
            return redirect(url_for('register'))

        # --- ĐOẠN LƯU VÀO DATABASE (GIỮ NGUYÊN) ---
        if User.query.filter_by(username=username).first():
            flash('Username already exists', 'warning')
            return redirect(url_for('register'))
        
        new_user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        
        # Thông báo khác nhau cho ngầu
        if username.lower() == 'admin':
            flash('Welcome, Boss! Admin account created.', 'success')
        else:
            flash('Registration successful! Please login.', 'success')
            
        return redirect(url_for('login'))
        
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- 7. ROUTES CHÍNH (LOGIC CŨ + LOGIN REQUIRED) ---
@app.route('/')
@login_required
def index():
    return render_template('index.html', all_styles=Style.query.all(), previous_inputs={}, user=current_user)

@app.route('/generate-story', methods=['POST'])
@login_required
def handle_generation():
    api_key = configure_ai()
    if not api_key: return jsonify({"story_result": "ERROR: API Key missing."}), 500
    
    data = request.form
    all_styles = {s.name: s.content for s in Style.query.all()}
    selected_styles = [all_styles[name] for name in data.getlist('selected_styles') if name in all_styles]
    
    inputs = {
        "idea": data.get('idea'), "vocab": [v.strip() for v in data.get('vocab_str', '').split(',') if v.strip()],
        "level": data.get('cefr_level'), "count": data.get('word_count'), "theme": data.get('theme'),
        "main_char": data.get('main_char'), "setting": data.get('setting'),
        "style_samples": selected_styles, "negative_keywords": data.get('negative_keywords'),
        "target_audience": data.get('target_audience'),
        "num_support": data.get('num_support_char')
    }
    
    if not inputs['idea']: return jsonify({"story_result": "ERROR: Idea is required."}), 400

    prompt = create_prompt_for_ai(inputs)
    return jsonify({"story_result": generate_story_ai(api_key, prompt)})
@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if request.method == 'POST':
        username = request.form['username']
        secret_code = request.form['secret_code']
        new_password = request.form['new_password']

        # --- CẤU HÌNH MÃ ---
        TEACHER_CODE = "GV_VIP_2025"
        ADMIN_CODE = "BOSS_ONLY_999" # Phải khớp với bên trên
        # -------------------

        # 1. Kiểm tra User có tồn tại không
        user = User.query.filter_by(username=username).first()
        if not user:
            flash('Username not found.', 'warning')
            return redirect(url_for('reset_password'))

        # 2. Logic Phân Quyền Reset
        if user.username == 'admin':
            # Muốn reset nick Admin thì PHẢI dùng mã Admin
            if secret_code != ADMIN_CODE:
                flash('Only the Boss Key can reset Admin password!', 'danger')
                return redirect(url_for('reset_password'))
        else:
            # Nick thường thì dùng mã nào cũng được (miễn là đúng 1 trong 2)
            if secret_code not in [TEACHER_CODE, ADMIN_CODE]:
                flash('Wrong Secret Code!', 'danger')
                return redirect(url_for('reset_password'))

        # 3. Đổi mật khẩu
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        
        flash('Password reset successful!', 'success')
        return redirect(url_for('login'))

    return render_template('reset_password.html')

# --- COMIC ROUTES ---
@app.route('/create-comic/<int:story_id>', methods=['POST'])
@login_required
def create_comic_direct(story_id):
    story = Story.query.get_or_404(story_id)
    # Bảo vệ: Chỉ chủ nhân truyện mới được tạo comic
    if story.user_id != current_user.id:
        return jsonify({"error": "Unauthorized: You do not own this story."}), 403

    api_key = configure_ai()
    try:
        prompt = create_comic_script_prompt(story.content)
        script_json_str = generate_story_ai(api_key, prompt)
        
        # Clean JSON Logic
        clean_json = script_json_str
        if "```json" in clean_json: clean_json = clean_json.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_json: clean_json = clean_json.split("```")[1].split("```")[0].strip()
        
        data = json.loads(clean_json)
        if 'panels' in data:
            panels_data = data['panels']
            back_cover = data.get('back_cover', {})
        else:
            panels_data = data
            back_cover = {"summary": "Read to find out!", "theme": "Story", "level": "Unknown"}

        final_panels = []
        for panel in panels_data:
            final_panels.append({
                "panel_number": panel['panel_number'],
                "image_url": "", 
                "prompt": panel.get('visual_description') or panel.get('prompt'),
                "caption": panel.get('caption', '')
            })
        
        final_panels.append({
            "panel_number": 999,
            "image_url": "",
            "prompt": "BACK_COVER_DATA",
            "caption": json.dumps(back_cover)
        })

        new_comic = Comic(story_id=story_id, panels_content=json.dumps(final_panels))
        db.session.add(new_comic)
        db.session.commit()
        
        return jsonify({"success": True, "redirect_url": url_for('view_comic', comic_id=new_comic.id)})
    except Exception as e:
        print(f"Comic Gen Error: {e}")
        return jsonify({"error": f"AI Error: {e}"}), 500

@app.route('/upload-panel-image', methods=['POST'])
@login_required
def upload_panel_image():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No selection"}), 400
    if file:
        comic_id = request.form.get('comic_id')
        panel_num = request.form.get('panel_number')
        
        # Check ownership comic
        comic = Comic.query.get(comic_id)
        if not comic or comic.story.user_id != current_user.id:
            return jsonify({"error": "Unauthorized"}), 403

        filename = f"comic_{comic_id}_p{panel_num}_{uuid.uuid4().hex[:6]}.png"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        new_url = f"/static/uploads/{filename}"
        
        panels = json.loads(comic.panels_content)
        for p in panels:
            if str(p['panel_number']) == str(panel_num): p['image_url'] = new_url; break
        comic.panels_content = json.dumps(panels)
        db.session.commit()
        return jsonify({"url": new_url})
    return jsonify({"error": "Upload failed"}), 500

@app.route('/view-comic/<int:comic_id>')
@login_required
def view_comic(comic_id):
    comic = Comic.query.get_or_404(comic_id)
    if comic.story.user_id != current_user.id:
        flash("You do not have permission to view this comic.", "danger")
        return redirect(url_for('saved_stories_page'))
    
    panels = json.loads(comic.panels_content)
    return render_template('view_comic.html', panels=panels, title=comic.story.title, comic_id=comic.id, user=current_user)

# --- OTHER ROUTES ---
@app.route('/styles')
@login_required
def styles_page(): return render_template('manage_styles.html', styles=Style.query.all(), user=current_user)

@app.route('/add-style', methods=['POST'])
@login_required
def add_style(): 
    db.session.add(Style(name=request.form['style_name'], content=request.form['style_content']))
    db.session.commit()
    return redirect(url_for('styles_page'))

@app.route('/delete-style', methods=['POST'])
@login_required
def delete_style(): 
    s = Style.query.filter_by(name=request.form['style_to_delete']).first()
    db.session.delete(s) if s else None
    db.session.commit()
    return redirect(url_for('styles_page'))

@app.route('/saved-stories')
@login_required
def saved_stories_page():
    # CHỈ HIỆN TRUYỆN CỦA NGƯỜI DÙNG HIỆN TẠI
    user_stories = Story.query.filter_by(user_id=current_user.id).order_by(Story.id.desc()).all()
    return render_template('saved_stories.html', stories=user_stories, user=current_user)

@app.route('/save-story', methods=['POST'])
@login_required
def handle_save_story():
    content = request.form.get('story_content', '')
    title = "Untitled"
    first_line = content.strip().split('\n')[0]
    if "#" in first_line: title = first_line.replace('#', '').strip()
    
    new_story = Story(title=title, content=content, user_id=current_user.id)
    db.session.add(new_story)
    db.session.commit()
    return redirect(url_for('saved_stories_page'))

@app.route('/delete-story', methods=['POST'])
@login_required
def handle_delete_story(): 
    s = Story.query.get(request.form.get('story_id'))
    if s and s.user_id == current_user.id:
        db.session.delete(s)
        db.session.commit()
    return redirect(url_for('saved_stories_page'))

@app.route('/edit-story/<int:story_id>', methods=['GET', 'POST'])
@login_required
def edit_story_page(story_id):
    s = Story.query.get_or_404(story_id)
    if s.user_id != current_user.id: return redirect(url_for('saved_stories_page'))
    
    if request.method == 'POST': 
        s.title = request.form['title']
        s.content = request.form['content']
        db.session.commit()
        return redirect(url_for('saved_stories_page'))
    return render_template('edit_story.html', story=s, user=current_user)

@app.route('/translate-story')
@login_required
def translate_page(): return render_template('translate_story.html', user=current_user)

@app.route('/handle-translation', methods=['POST'])
@login_required
def handle_translation():
    api_key = configure_ai()
    if not api_key: return jsonify({"story_result": "ERROR: API Key missing."}), 500
    data = request.form
    inputs = {"folktale_name": data.get('folktale_name'), "level": data.get('cefr_level'), "count": data.get('word_count'), "target_audience": data.get('target_audience')}
    prompt = create_translation_prompt(inputs)
    return jsonify({"story_result": generate_story_ai(api_key, prompt)})

@app.route('/add-quiz-to-saved', methods=['POST'])
@login_required
def add_quiz_to_saved():
    story = Story.query.get(request.form.get('story_id'))
    if not story or story.user_id != current_user.id: return redirect(url_for('saved_stories_page'))

    quiz_type = request.form.get('quiz_type')
    api_key = configure_ai()
    try:
        prompt = create_quiz_only_prompt(story.content, quiz_type)
        quiz_content = generate_story_ai(api_key, prompt)
        story.content += f"\n\n--- EXTRA QUIZ ({quiz_type.upper()}) ---\n{quiz_content}"
        db.session.commit()
        flash(f"Added {quiz_type} quiz!", "success")
    except Exception as e:
        flash(f"Error creating quiz: {e}", "danger")
    return redirect(url_for('saved_stories_page'))



# --- ADMIN DASHBOARD PRO ---
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    if current_user.username != 'admin': return "Access Denied", 403
    users = User.query.all()
    return render_template('admin.html', users=users)

# Chức năng 1: Reset mật khẩu về 123456
@app.route('/admin/reset-pass/<int:user_id>', methods=['POST'])
@login_required
def admin_reset_pass(user_id):
    if current_user.username != 'admin': return "Access Denied", 403
    user = User.query.get(user_id)
    if user:
        user.password_hash = generate_password_hash("123456") # Mật khẩu mặc định
        db.session.commit()
        flash(f"Password for {user.username} reset to '123456'", "success")
    return redirect(url_for('admin_dashboard'))

# Chức năng 2: Khóa / Mở khóa tài khoản
@app.route('/admin/toggle-lock/<int:user_id>', methods=['POST'])
@login_required
def admin_toggle_lock(user_id):
    if current_user.username != 'admin': return "Access Denied", 403
    user = User.query.get(user_id)
    if user and user.username != 'admin': # Không được khóa chính mình
        user.is_locked = not user.is_locked # Đảo ngược trạng thái
        status = "LOCKED" if user.is_locked else "UNLOCKED"
        db.session.commit()
        flash(f"User {user.username} is now {status}", "warning")
    return redirect(url_for('admin_dashboard'))

# Chức năng 3: Xóa tài khoản
@app.route('/admin/delete/<int:user_id>', methods=['POST'])
@login_required
def admin_delete_user(user_id):
    if current_user.username != 'admin': return "Access Denied", 403
    user = User.query.get(user_id)
    if user and user.username != 'admin': # Không được xóa chính mình
        # Xóa hết truyện và comic của user đó trước
        Story.query.filter_by(user_id=user.id).delete()
        db.session.delete(user)
        db.session.commit()
        flash(f"User {user.username} deleted permanently.", "danger")
    return redirect(url_for('admin_dashboard'))

# --- AUTO CREATE DB ---
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true': webbrowser.open_new('http://127.0.0.1:5000/')
    app.run(debug=True, port=5000)