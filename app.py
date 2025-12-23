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

# --- THÊM ĐOẠN NÀY ĐỂ FIX LỖI SSL CONNECTION ---
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,   # Quan trọng: Kiểm tra kết nối trước khi dùng
    "pool_recycle": 300,     # Tái tạo kết nối mỗi 300 giây (5 phút)
}
# -----------------------------------------------

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
    name = db.Column(db.String(100), nullable=False) # Bỏ unique=True để mỗi user có thể đặt tên giống nhau
    content = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False) # Thêm dòng này
    
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
    try:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.DOTALL)
        if match:
            text = match.group(1)
        else:
            start_match = re.search(r"[\{\[]", text)
            if start_match:
                start_idx = start_match.start()
                end_idx = max(text.rfind('}'), text.rfind(']'))
                if end_idx > start_idx:
                    text = text[start_idx : end_idx + 1]
            else:
                return None

        text = re.sub(r'//.*', '', text)
        text = re.sub(r',\s*([\]}])', r'\1', text)
        return json.loads(text)
    except Exception as e:
        print(f"JSON Parsing Error: {e}")
        return None
    
# --- 4. ADVANCED PROMPT ENGINEERING ---

CEFR_LEVEL_GUIDELINES = {
    "PRE A1": "Simple Present (be/have/action). Short sentences (3-6 words). Focus on visual actions.",
    "A1": "Present Simple/Continuous. Basic conjunctions (and, but). Dialogues are simple Q&A.",
    "A2": "Past Simple, Future (will/going to). Adverbs of frequency. Coordinated sentences.",
    "B1": "Narrative tenses (Past Continuous), Conditionals (1 & 2), Reasons (because/so). Expressing feelings/opinions.",
    "B2": "Passive voice, Reported speech, Relative clauses. Nuanced vocabulary and abstract ideas.",
    "C1": "Complex sentence structures, Inversion, Idiomatic expressions. Literary tone.",
    "C2": "Sophisticated style, Implicit meaning, Cultural references, Irony/Humor."
}

# --- KHO GIỌNG VĂN MẪU THEO LEVEL (LITERARY STYLES) ---
LITERARY_STYLES = {
    "PRE A1": [
        "Style of Eric Carle: Very simple, repetitive, focuses on nature, colors, and sensory details.",
        "Style of Margaret Wise Brown (Goodnight Moon): Gentle, rhythmic, soothing, listing objects in the room.",
        "Style of Mo Willems: Dialogue-heavy, simple, repetitive but expressive and funny."
    ],
    "A1": [
        "Style of Arnold Lobel (Frog and Toad): Simple but warm friendship stories, cozy atmosphere.",
        "Style of Beatrix Potter: Gentle, pastoral, focuses on small animals and rural settings.",
        "Style of Dr. Seuss (Prose version): Whimsical, playful, simple vocabulary but creative concepts."
    ],
    "A2": [
        "Style of Roald Dahl: Mischievous, energetic, vivid adjectives, funny exaggerations of characters.",
        "Style of Enid Blyton: Clear adventure, group of friends, descriptive but accessible.",
        "Style of Jeff Kinney (Wimpy Kid): Casual diary format, relatable school life struggles, humorous."
    ],
    "B1": [
        "Style of Ernest Hemingway: Short, punchy sentences. Focus on action and concrete details. No fluffy adjectives.",
        "Style of E.B. White (Charlotte's Web): Clear, elegant, touching, focuses on nature and loyalty.",
        "Style of R.L. Stine (Goosebumps): Suspenseful, cliffhangers, engaging plot twists (good for mysteries)."
    ],
    "B2": [
        "Style of Mark Twain: Folksy, observational, rich in local color and dialect nuances.",
        "Style of C.S. Lewis: Descriptive, slightly magical tone, clear moral compass.",
        "Style of Neil Gaiman (Coraline): Atmospheric, slightly dark/mysterious, rich imagery."
    ],
    "C1": [
        "Style of Jane Austen: Social observation, irony, complex sentence structures, focus on manners/relationships.",
        "Style of Sherlock Holmes (Conan Doyle): Deductive, analytical, detailed descriptions of settings.",
        "Style of Jack London: Raw nature, survival, intense description of the environment."
    ],
    "C2": [
        "Style of Oscar Wilde: Witty, aesthetic, sophisticated vocabulary, paradoxical humor.",
        "Style of Edgar Allan Poe: Melancholic, poetic, complex grammar, psychological depth.",
        "Style of Virginia Woolf: Stream of consciousness, focus on internal thoughts and fleeing moments."
    ]
}

def create_prompt_for_ai(inputs):
    cefr_level = inputs['level'].upper()
    vocab_list_str = ", ".join(inputs['vocab'])
    
    try:
        word_count = int(inputs['count'])
    except:
        word_count = 250
        
    setting_val = inputs['setting'].strip()
    setting_instr = f"**SETTING:** {setting_val}" if setting_val else "**SETTING:** A realistic setting in Vietnam. Atmosphere is key."

    # --- 1. CHỌN STYLE TỰ ĐỘNG THEO LEVEL ---
    suggested_styles = LITERARY_STYLES.get(cefr_level, [])
    style_selection_instr = ""
    
    if suggested_styles:
        style_list_str = "\n".join([f"- {s}" for s in suggested_styles])
        style_selection_instr = f"""
    **LITERARY VOICE (CRITICAL):**
    Choose ONE of the following styles that BEST fits the story idea below:
    {style_list_str}
    -> **Apply the chosen style consistently.**
    """

    # --- 2. LOGIC CẤU TRÚC (UPDATE: Thêm Narrative Flow) ---
    target_audience = inputs.get('target_audience', 'General')

    if target_audience == 'Children' and cefr_level in ["PRE A1", "A1", "A2"]:
        structure_type = "PICTURE BOOK"
        if word_count < 150: num_pages = "4-5"
        elif word_count < 300: num_pages = "6-8"
        else: num_pages = "8-10"
            
        structure_instr = f"""
        **STRUCTURE: PICTURE BOOK FORMAT**
        - Divide the story into **{num_pages} PAGES**.
        - Label each part clearly as: `--- PAGE [X] ---`
        - **IMPORTANT:** Write a meaningful paragraph (3-5 sentences) per page.
        - **FLOW:** Ensure smooth transitions between pages. Use connecting words (Then, Next, Suddenly) so the story reads as one continuous narrative, not disjointed scenes.
        """
        opening_rule = "Start with a **# Title**. Then immediately start with `--- PAGE 1 ---`."
        
    elif word_count < 400:
        structure_type = "SHORT STORY (Continuous)"
        structure_instr = """
        **STRUCTURE: CONTINUOUS STORY**
        - Do NOT use Chapter headings.
        - Start directly with the story content after the Title.
        - Organize into clear paragraphs.
        """
        opening_rule = "Start with a **# Title**. Then immediately start the story text."
    else:
        structure_type = "CHAPTER BOOK"
        structure_instr = """
        **STRUCTURE: CHAPTERS**
        - Divide into **3-5 CHAPTERS**. Label: `### CHAPTER [X]: [Title]`
        - **IMPORTANT:** The story must start immediately with **CHAPTER 1**.
        """
        opening_rule = "Start with a **# Title**. Immediately follow with **CHAPTER 1**. Introduce the character INSIDE Chapter 1."

    repetition_rule = "Weave target words into the story naturally (approx 3-5 times each)."

    # --- 3. XỬ LÝ MAGIC DUST (Giữ nguyên) ---
    support_instr = ""
    raw_num = inputs.get('num_support')
    if raw_num and str(raw_num).strip(): 
        try:
            num = int(raw_num)
            if num > 0:
                support_instr = f"- **Supporting Characters:** Include exactly {num} supporting character(s). Ensure meaningful interaction."
            else:
                support_instr = "- **Supporting Characters:** No supporting characters. Focus on internal thoughts."
        except:
            support_instr = "- **Supporting Characters:** Automatically introduce 1-2 supporting characters. **MANDATORY:** Include natural dialogue."
    else:
        support_instr = "- **Supporting Characters:** Automatically introduce 1-2 supporting characters. **MANDATORY:** Include natural dialogue."

    negative_instr = ""
    if inputs.get('negative_keywords'):
        negative_instr = f"- **NEGATIVE CONSTRAINTS:** Strictly AVOID: {inputs['negative_keywords']}."

    user_style_instr = ""
    if inputs.get('style_samples'):
        style_sample_text = inputs['style_samples'][:500].replace("\n", " ")
        user_style_instr = f"- **USER OVERRIDE STYLE:** MIMIC this specific tone: '{style_sample_text}...'"

    # --- 4. MASTER PROMPT ---
    prompt = f"""
    **Role:** Best-selling Author of Graded Readers.
    **Goal:** Write a {structure_type} that is engaging, emotional, and educational.
    
    **CORE INPUTS:**
    - Level: {cefr_level}
    - Length: ~{word_count} words.
    - Theme: {inputs['theme']}
    - Main Character: {inputs.get('main_char', 'A relatable character')}
    {support_instr}
    
    **MANDATORY GUIDELINES:**
    
    1. **OPENING & STRUCTURE:** - {opening_rule}
       - {structure_instr}
    
    2. **VOCABULARY INTEGRATION:**
       - **Target Words:** [{vocab_list_str}]
       - {repetition_rule}
       - Do NOT use backticks/bold for target words.
    
    3. {setting_instr}
    
    4. **GRAMMAR & TONE:**
       - **Grammar Level:** {CEFR_LEVEL_GUIDELINES.get(cefr_level, "Standard grammar")}
       - **CRITICAL:** Even at low levels (A1/A2), use **NATURAL English phrasing**. ALWAYS use proper articles (a, an, the) and pronouns. Do NOT write in "pidgin" or broken English (e.g., write "He stays in his bedroom", NOT "He stay in bedroom").
       {style_selection_instr}
       - **Tone:** Encouraging, Relatable, Human.
    
    5. **ADDITIONAL CONSTRAINTS:**
       {negative_instr}
       {user_style_instr}
       - **LANGUAGE:** Write in standard English. Use English terms for family members (Mom, Dad, Grandma) unless strictly instructed otherwise.

    **OUTPUT FORMAT:**
    # [Creative Title]
    [Story content...]
    ---
    Graded Definitions ({cefr_level})
    *Format:*
    - word: definition.
    """
    return prompt
    
def create_comic_script_prompt(story_content):
    return f"""
    **Role:** Cinematic Art Director.
    **Task:** Convert the story below into a list of 12 visual descriptions for image generation.
    **INPUT STORY:** {story_content}
    
    **VISUAL RULES:**
    - **Style:** Disney/Pixar 3D style, vibrant colors, expressive lighting.
    - **Consistency:** Use specific descriptions (e.g., "A 4-year-old Vietnamese boy named Nhan, wearing a blue t-shirt").
    - **Content:** Create visuals that match the emotional tone of the story.

    **OUTPUT FORMAT (STRICT JSON ONLY):**
    Return a valid JSON object. Do not add any introductory text or markdown formatting outside the JSON.
    
    {{
      "panels": [ 
        {{ 
            "panel_number": 1, 
            "visual_description": "Detailed description of the scene...", 
            "caption": "Short text from the story" 
        }}
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

def create_pedagogical_quiz_prompt(story_content, quiz_preference):
    return f"""
    **Role:** Quiz Generator Engine.
    **MODE:** STRICT OUTPUT ONLY.
    **Forbidden:** Do NOT include internal reasoning, notes about distractors, or conversational filler (e.g., "Here is the quiz", "Note: I chose...").
    
    **Task:** Create a 3-stage quiz for the story below.
    
    **INPUT STORY:**
    {story_content}

    **STRUCTURE:**

    PART 1: CONTROLLED PRACTICE (Recall)
    *Format:* Based on '{quiz_preference}' (mcq/tf/mix/open).
    - Create 5 questions.

    PART 2: LESS CONTROLLED PRACTICE (Vocabulary)
    *Format:* **Gap Fill**.
    - Create a short summary text with 5-6 blanks.
    - **CRITICAL:** Use standard underscores for blanks like this: `_______ (1)`.
    - **MANDATORY:** Provide the Word Bank on a SINGLE LINE exactly like this format (no tables):
      `[[WORD BANK: word1, word2, word3, word4, word5, distractor1]]`

    PART 3: FREE PRACTICE (Production)
    1. **Discussion:** 1 Open-ended question connecting to real life.
    2. **Creative Writing:** 1 Prompt (rewrite ending, dialogue, etc.).

    **ANSWER KEY (At the bottom)**
    - Provide ONLY the answers.
    - Do NOT explain why an answer was chosen.
    - Format strictly:
      Part 1: 1. A, 2. B...
      Part 2: 1. word...
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
        
        if user and user.username.lower() == 'admin':
            system_pin_hash = os.environ.get('ADMIN_PIN_HASH')
            if not system_pin_hash or not check_password_hash(system_pin_hash, admin_pin):
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
        
        # Lấy danh sách mã hợp lệ từ biến môi trường
        valid_codes = [os.environ.get('REGISTRATION_CODE_BOSS'), os.environ.get('REGISTRATION_CODE_VIP')]
        # Lọc bỏ các giá trị rỗng (None) nếu chưa set biến môi trường
        valid_codes = [c for c in valid_codes if c] 
        
        # --- SỬA LỖI: Thêm thông báo (Flash) trước khi redirect ---
        
        # 1. Kiểm tra Mã Đăng Ký (Secret Code)
        if not valid_codes:
            # Trường hợp quên set biến môi trường trên server
            flash('Lỗi hệ thống: Admin chưa cấu hình Mã Đăng Ký (Env Var).', 'danger')
            return redirect(url_for('register'))

        if code not in valid_codes: 
            flash('Mã đăng ký (Secret Code) không đúng! Vui lòng hỏi Admin.', 'danger') # <--- Báo lỗi sai code
            return redirect(url_for('register'))
            
        # 2. Kiểm tra việc giả danh Admin
        if username.lower() == 'admin' and code != "BOSS_ONLY_999": 
            flash('Bạn không thể đăng ký tên "admin" với mã này.', 'danger')
            return redirect(url_for('register'))
            
        # 3. Kiểm tra tên đăng nhập đã tồn tại chưa
        if User.query.filter_by(username=username).first(): 
            flash('Tên đăng nhập này đã có người dùng. Hãy chọn tên khác!', 'danger') # <--- Báo lỗi trùng tên
            return redirect(url_for('register'))
        
        # --- NẾU ỔN HẾT THÌ MỚI TẠO USER ---
        try:
            new_user = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(new_user)
            db.session.commit()
            flash('Đăng ký thành công! Vui lòng đăng nhập.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            print(f"Error creating user: {e}")
            flash('Lỗi database khi tạo tài khoản. Vui lòng thử lại.', 'danger')
            return redirect(url_for('register'))

    return render_template('register.html')

@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    user_styles = Style.query.filter_by(user_id=current_user.id).all()
    return render_template('index.html', all_styles=user_styles, previous_inputs={}, user=current_user)

@app.route('/generate-story', methods=['POST'])
@login_required
def handle_generation():
    api_key = configure_ai()
    if not api_key: return jsonify({"story_result": "API Key Missing"}), 500
    data = request.form
    
    selected_style_names = request.form.getlist('selected_styles')
    style_content_str = ""
    if selected_style_names:
        # Sửa truy vấn: Lọc theo tên VÀ user_id
        styles = Style.query.filter(Style.name.in_(selected_style_names), Style.user_id == current_user.id).all()
        style_content_str = "\n".join([s.content for s in styles])

    inputs = {
        "idea": data.get('idea'), 
        "vocab": data.get('vocab_str', '').split(','),
        "level": data.get('cefr_level'), 
        "count": data.get('word_count'), 
        "theme": data.get('theme'),
        "main_char": data.get('main_char'), 
        "setting": data.get('setting'),
        "style_samples": style_content_str,
        "negative_keywords": data.get('negative_keywords'),
        "target_audience": data.get('target_audience'), 
        "num_support": data.get('num_support_char')
    }
    
    prompt = create_prompt_for_ai(inputs)
    story_content = generate_story_ai(api_key, prompt)
    
    if "ERROR" in story_content:
        return jsonify({"story_result": story_content})

    quiz_type = data.get('quiz_type')
    if quiz_type and quiz_type != 'none':
        quiz_prompt = create_pedagogical_quiz_prompt(story_content, quiz_type)
        quiz_content = generate_story_ai(api_key, quiz_prompt)
        story_content += f"\n\n\n{'='*20}\n## 🎓 PEDAGOGICAL WORKSHEET\n{'='*20}\n\n{quiz_content}"

    return jsonify({"story_result": story_content})

@app.route('/create-comic/<int:story_id>', methods=['POST'])
@login_required
def create_comic_direct(story_id):
    story = Story.query.get_or_404(story_id)
    api_key = configure_ai()
    
    try:
        clean_story_content = story.content
        separator = "## 🎓 PEDAGOGICAL WORKSHEET"
        if separator in clean_story_content:
            clean_story_content = clean_story_content.split(separator)[0]

        char_desc = "A relatable character"
        try:
            if story.prompt_data:
                saved_inputs = json.loads(story.prompt_data)
                raw_char = saved_inputs.get('main_char', '')
                if raw_char:
                    char_desc = f"{raw_char}, distinct facial features, wearing a signature outfit, consistent character"
        except: pass
            
        consistency_prompt = f"IDENTITY: {char_desc}. (Keep facial features, hair style, and clothing EXACTLY the same in every shot)."

        ai_response_text = generate_story_ai(api_key, create_comic_script_prompt(clean_story_content))
        data = robust_json_extract(ai_response_text)
        
        if not data: return jsonify({"error": "AI Error. Please try again."}), 500

        panels_data = data.get('panels', data.get('scenes', data))
        if not isinstance(panels_data, list):
             if isinstance(panels_data, dict): panels_data = [panels_data]
             else: return jsonify({"error": "JSON Error."}), 500

        final_panels = []

        for panel in panels_data:
            raw_action = panel.get('visual_description') or panel.get('description') or "Scene"
            for w in ["comic", "panel", "page", "grid", "speech bubble", "text"]: 
                raw_action = raw_action.replace(w, "image")
            
            final_prompt = (
                f"**[1] CHARACTER:** {consistency_prompt} "
                f"**[2] SCENE ACTION:** {raw_action}. "
                f"**[3] STYLE:** 3D Disney Pixar Animation style, 8k render, soft lighting. "
                f"--ar 3:2 --no text speech bubbles comic grid"
            )
            
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
    scenes = [p['prompt'] for p in panels]
    return jsonify({"batch_prompt": " ".join(scenes)})

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
    # Sửa truy vấn style tại đây
    return render_template('index.html', all_styles=Style.query.filter_by(user_id=current_user.id).all(), previous_inputs=prev_inputs, user=current_user)

@app.route('/styles')
@login_required
def styles_page(): 
    return render_template('manage_styles.html', styles=Style.query.filter_by(user_id=current_user.id).all(), user=current_user)

@app.route('/add-style', methods=['POST'])
@login_required
def add_style():
    content = request.form.get('style_content', '')
    if request.files.get('style_file'): content = extract_text_from_file(request.files['style_file']) or content
    
    # Kiểm tra trùng tên (chỉ trong phạm vi user đó)
    existing = Style.query.filter_by(name=request.form['style_name'], user_id=current_user.id).first()
    if existing:
        flash('Style name already exists!', 'warning')
    else:
        db.session.add(Style(name=request.form['style_name'], content=content, user_id=current_user.id))
        db.session.commit()
    return redirect(url_for('styles_page'))

@app.route('/delete-style', methods=['POST'])
@login_required
def delete_style():
    s = Style.query.filter_by(name=request.form['style_to_delete'], user_id=current_user.id).first()
    if s: db.session.delete(s); db.session.commit()
    return redirect(url_for('styles_page'))

@app.route('/saved-stories')
@login_required
def saved_stories_page(): return render_template('saved_stories.html', stories=Story.query.filter_by(user_id=current_user.id).order_by(Story.id.desc()).all(), user=current_user)

@app.route('/save-story', methods=['POST'])
@login_required
def handle_save_story():
    content = request.form.get('story_content', '')
    
    # --- LOGIC MỚI: Tự động trích xuất tiêu đề thông minh hơn ---
    title = "Untitled Story"
    if content:
        # Tách các dòng
        lines = content.strip().split('\n')
        for line in lines:
            clean_line = line.strip()
            # Bỏ qua các dòng trống hoặc dòng tào lao của AI
            if not clean_line or "Here is" in clean_line or "Sure," in clean_line:
                continue
            
            # Nếu tìm thấy dòng có dấu # (Tiêu đề Markdown) hoặc dòng chữ bình thường đầu tiên
            title = clean_line.replace('#', '').replace('*', '').strip()
            break
            
        # Giới hạn độ dài tiêu đề để không bị lỗi database
        if len(title) > 100:
            title = title[:97] + "..."
    # -----------------------------------------------------------

    db.session.add(Story(
        title=title, 
        content=content, 
        user_id=current_user.id, 
        prompt_data=request.form.get('prompt_data_json')
    ))
    db.session.commit()
    return redirect(url_for('saved_stories_page'))

@app.route('/delete-story', methods=['POST'])
@login_required
def handle_delete_story():
    story_id = request.form.get('story_id')
    s = Story.query.get(story_id)
    
    if s and s.user_id == current_user.id:
        try:
            # 1. Xóa tất cả Comic liên quan đến truyện này trước
            Comic.query.filter_by(story_id=s.id).delete()
            
            # 2. Sau đó mới xóa Truyện
            db.session.delete(s)
            db.session.commit()
            flash('Story deleted successfully.', 'success')
        except Exception as e:
            db.session.rollback() # Hoàn tác nếu có lỗi
            print(f"Error deleting story: {e}")
            flash('Error deleting story. Please try again.', 'danger')
            
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
        quiz_type = request.form.get('quiz_type')
        api_key = configure_ai()
        prompt = create_pedagogical_quiz_prompt(s.content, quiz_type)
        quiz_content = generate_story_ai(api_key, prompt)
        s.content += f"\n\n\n{'='*20}\n## 🎓 PEDAGOGICAL WORKSHEET\n{'='*20}\n\n{quiz_content}"
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

@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    if request.method == 'POST':
        username = request.form['username']
        code = request.form['secret_code']
        new_password = request.form['new_password']
        
        valid_codes = [os.environ.get('REGISTRATION_CODE_BOSS'), os.environ.get('REGISTRATION_CODE_VIP')]
        valid_codes = [c for c in valid_codes if c] 

        if code not in valid_codes:
            flash('Invalid Secret Code provided.', 'danger')
            return redirect(url_for('reset_password'))

        user = User.query.filter_by(username=username).first()
        if user:
            user.password_hash = generate_password_hash(new_password)
            db.session.commit()
            flash('Password reset successfully! Please login.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Username not found.', 'danger')
            return redirect(url_for('reset_password'))

    return render_template('reset_password.html')

@app.route('/fix-style-db')
def fix_style_db():
    try:
        # Lệnh này chỉ xóa bảng Style cũ đi
        Style.__table__.drop(db.engine)
        
        # Lệnh này tạo lại bảng Style mới (theo code mới có user_id)
        # Các bảng khác (User, Story...) đã có rồi nên sẽ không bị ảnh hưởng
        db.create_all()
        
        return "Thành công! Đã reset bảng Style. Tài khoản User vẫn an toàn."
    except Exception as e:
        return f"Lỗi: {e}"
        
if __name__ == '__main__':
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true': webbrowser.open_new('http://127.0.0.1:5000/')
    app.run(debug=True, port=5000)









