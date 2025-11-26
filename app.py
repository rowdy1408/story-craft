import os
import json
import http.client
import time
import sys
import webbrowser
import requests
import uuid
import urllib.parse
import shutil 
import random 
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from markupsafe import Markup
from flask_sqlalchemy import SQLAlchemy
from dotenv import load_dotenv

# --- 1. CẤU HÌNH BAN ĐẦU ---
if getattr(sys, 'frozen', False):
    base_dir = sys._MEIPASS
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

env_path = os.path.join(base_dir, 'keys.env') 
load_dotenv(dotenv_path=env_path)

app = Flask(__name__, 
            template_folder=os.path.join(base_dir, 'templates'), 
            static_folder=os.path.join(base_dir, 'static'))
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'secret_key_123')

UPLOAD_FOLDER = os.path.join(base_dir, 'static', 'uploads')
if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)

db_path = os.path.join(base_dir, 'instance', 'story_project.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
instance_folder = os.path.join(base_dir, 'instance')
if not os.path.exists(instance_folder): os.makedirs(instance_folder)
db = SQLAlchemy(app)

# --- 2. MODELS ---
class Style(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    content = db.Column(db.Text, nullable=False)

class Story(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    content = db.Column(db.Text, nullable=False)

class Comic(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.Integer, db.ForeignKey('story.id'), nullable=False)
    panels_content = db.Column(db.Text, nullable=False)
    story = db.relationship('Story', backref=db.backref('comics', lazy=True))

# --- 3. HELPER & CONFIG ---
def configure_ai():
    try:
        api_key = os.environ["GOOGLE_API_KEY"]
        if not api_key:
            print("ERROR: GOOGLE_API_KEY is empty.")
            return None
        return api_key
    except KeyError:
        print("ERROR: GOOGLE_API_KEY not found.")
        return None

# --- 4. GỌI AI (YESCALE WRAPPER) ---
def generate_story(api_key, prompt):
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
             # CLEANUP: Xóa mọi dấu ** hoặc * bao quanh từ vựng nếu AI vẫn cố tình thêm vào
             # (Dù prompt đã cấm, nhưng thêm lớp bảo vệ này cho chắc chắn)
             return content.replace('**', '') 
        else:
             return f"ERROR: Invalid response structure. {data}"

    except Exception as e:
        return f"ERROR: System error: {e}"

# --- 5. PROMPTS CHUYÊN SÂU ---

CEFR_LEVEL_GUIDELINES = {
    "PRE A1": """
    - **Grammar:** Strict Pre-A1 (Present Simple, Be, Have, Imperatives). Avg 3-8 words/sentence. NO complex sentences.""",
    "A1": """- **Grammar:** Simple Present & Continuous, Can, Have got. Short dialogues. Avg 6-10 words/sentence.""",
    "A2": """- **Grammar:** Simple Past, Future, Comparatives, Modals (must, should). Avg 8-12 words/sentence.""",
    "B1": """- **Grammar:** Narrative tenses, Conditionals, Relative clauses. Show, don't tell.""",
    "B2": """- **Grammar:** Passive voice, Reported speech, Complex sentences.""",
    "C1": """- **Style:** Literary, symbolic, advanced connectors.""",
    "C2": """- **Style:** Sophisticated, implicit meanings, philosophical themes."""
}

# 1. CẬP NHẬT HÀM TẠO STORY PROMPT
def create_prompt_for_ai(inputs):
    vocab_list_str = ", ".join(inputs['vocab'])
    cefr_level = inputs['level'].upper()
    
    raw_audience = inputs.get('target_audience', 'Children')
    audience_type = "CHILDREN"
    if any(x in raw_audience.lower() for x in ['adult', 'business', 'office', 'student']):
        audience_type = "ADULT"

    # --- SỬA LOGIC NHÂN VẬT PHỤ (CHỈ NHẬN SỐ LƯỢNG) ---
    raw_support = inputs.get('num_support', '').strip()
    support_instruction = ""
    
    if not raw_support or raw_support == '0':
        support_instruction = "Add 1-2 generic background characters if needed for realism (e.g., a crowd, a seller)."
    else:
        # Luôn xử lý như một con số
        support_instruction = f"Include exactly **{raw_support} generic supporting characters** (e.g., neighbor, driver) to interact with the Main Character."

    # (Giữ nguyên phần Setting Lock và Structure như cũ)
    setting_val = inputs['setting'].strip()
    setting_instruction = f"**SETTING LOCK:** Must be in **{setting_val}**." if setting_val else "Setting: Authentic Vietnamese context."

    structure_instruction = ""
    tone_instruction = ""

    if cefr_level in ["PRE A1", "A1"]:
        if audience_type == "ADULT":
            structure_instruction = "- **Structure:** Write **3-5 clear PARAGRAPHS**. NO 'Page' breaks. Use Elaboration."
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
    1. **POETIC TRANSLATION (Review carefully):** - Identify iconic rhymes/verses in the original story (e.g., "Bống bống bang bang...", "Vàng ảnh vàng anh...").
       - **DO NOT** translate these as plain prose.
       - **MUST** translate them into **English Rhyming Couplets** (AABB or ABAB rhyme scheme) to keep the magical feel.
       - *Example:* Instead of "Gold bird, come eat...", write: "Golden bird, golden bird, / My husband's coat must not be blurred."

    2. **CONSTRAINTS:**
       - **Level:** {cefr_level}.
       - **Length:** {inputs['count']} words.
       - **Grammar:** {level_guidelines}
       - **Culture:** Keep cultural terms (Tet, Betel nut) and explain them briefly in context.
    
    **OUTPUT:**
    # [English Title]
    [Story Content]
    ---
    ## Graded Definitions
    [Key terms defined]
    """
    return prompt

def create_quiz_only_prompt(story_text, quiz_type):
    return f"""
    **Role:** Educational Content Creator.
    **Task:** Create a Reading Comprehension Quiz for the story below.
    
    **Story:**
    {story_text}
    
    **Requirements:**
    - **Type:** {quiz_type} (MCQ = Multiple Choice, Open = Open Ended, Mix = Both).
    - **Quantity:** 5 Questions.
    - **Level:** Match the English level of the story.
    
    **Output Format:**
    ## Reading Quiz ({quiz_type})
    [List Questions]
    
    ---
    ## Answer Key
    [List Answers]
    """

def create_comic_script_prompt(story_content):
    return f"""
    **Role:** Professional Comic Book Director.
    **Task:** Convert the story into a Comic Script JSON.
    
    **INPUT STORY:**
    {story_content}
    
    **CRITICAL INSTRUCTIONS:**
    1. **STRUCTURE:** Create EXACTLY ONE Panel for EACH Page found in the story.
    
    2. **CONTENT:**
       - **Visual:** Detailed description for the artist. Keep character visuals consistent.
       - **Caption (CRITICAL):** Copy the text of the page verbatim. 
       - **FORMATTING RULE:** Do **NOT** add quotation marks (`""`) around the narrative text. Only use quotation marks if characters are actually speaking (dialogue).
       - *Bad Example:* "Nhân runs fast."
       - *Good Example:* Nhân runs fast.
       - *Good Example:* Mom says, "Run!"

    3. **BACK COVER:** Generate metadata for the back cover.

    **OUTPUT JSON FORMAT (Strictly JSON):**
    {{
      "panels": [
        {{
          "panel_number": 1,
          "visual_description": "...",
          "caption": "Text without extra quotes..."
        }},
        ...
      ],
      "back_cover": {{
        "summary": "Short summary.",
        "theme": "Theme",
        "level": "Level"
      }}
    }}
    """

# --- 6. ROUTES ---
@app.route('/')
def index(): return render_template('index.html', all_styles=Style.query.all(), previous_inputs={})

@app.route('/generate-story', methods=['POST'])
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
        "target_audience": data.get('target_audience')
    }
    
    if not inputs['idea']: return jsonify({"story_result": "ERROR: Idea is required."}), 400

    prompt = create_prompt_for_ai(inputs)
    return jsonify({"story_result": generate_story(api_key, prompt)})

# --- COMIC ROUTES ---
@app.route('/create-comic/<int:story_id>', methods=['POST'])
def create_comic_direct(story_id):
    story = Story.query.get_or_404(story_id)
    api_key = configure_ai()
    
    try:
        prompt = create_comic_script_prompt(story.content)
        script_json_str = generate_story(api_key, prompt)
        
        # Clean JSON
        clean_json = script_json_str
        if "```json" in clean_json: clean_json = clean_json.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_json: clean_json = clean_json.split("```")[1].split("```")[0].strip()
        
        data = json.loads(clean_json)
        
        # Xử lý cấu trúc cũ/mới (phòng hờ)
        if 'panels' in data:
            panels_data = data['panels']
            back_cover = data.get('back_cover', {})
        else:
            panels_data = data # Fallback nếu AI trả về list cũ
            back_cover = {"summary": "Read to find out!", "theme": "Story", "level": "Unknown"}

        final_panels = []
        for panel in panels_data:
            final_panels.append({
                "panel_number": panel['panel_number'],
                "image_url": "", 
                "prompt": panel.get('visual_description') or panel.get('prompt'),
                "caption": panel.get('caption', '')
            })
        
        # Lưu Back Cover info vào DB? 
        # Hiện tại Model Comic chỉ có `panels_content`. 
        # Trick: Mình sẽ lưu back_cover vào phần tử cuối cùng của list hoặc bọc cả list trong dict.
        # Để đơn giản và không sửa DB: Mình sẽ lưu back_cover là một phần tử đặc biệt trong list panels với panel_number = 999
        
        final_panels.append({
            "panel_number": 999, # Marker cho Back Cover
            "image_url": "", # Không cần ảnh, chỉ hiện text
            "prompt": "BACK_COVER_DATA",
            "caption": json.dumps(back_cover) # Lưu data vào caption
        })

        new_comic = Comic(story_id=story_id, panels_content=json.dumps(final_panels))
        db.session.add(new_comic)
        db.session.commit()
        
        return jsonify({"success": True, "redirect_url": url_for('view_comic', comic_id=new_comic.id)})
    except Exception as e:
        print(f"Comic Gen Error: {e}")
        return jsonify({"error": f"AI Error: {e}"}), 500

@app.route('/upload-panel-image', methods=['POST'])
def upload_panel_image():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No selection"}), 400
    if file:
        comic_id = request.form.get('comic_id'); panel_num = request.form.get('panel_number')
        filename = f"comic_{comic_id}_p{panel_num}_{uuid.uuid4().hex[:6]}.png"
        filepath = os.path.join(UPLOAD_FOLDER, filename); file.save(filepath)
        new_url = f"/static/uploads/{filename}"
        comic = Comic.query.get(comic_id)
        if comic:
            panels = json.loads(comic.panels_content)
            for p in panels:
                if str(p['panel_number']) == str(panel_num): p['image_url'] = new_url; break
            comic.panels_content = json.dumps(panels); db.session.commit()
            return jsonify({"url": new_url})
    return jsonify({"error": "Upload failed"}), 500

@app.route('/view-comic/<int:comic_id>')
def view_comic(comic_id):
    comic = Comic.query.get_or_404(comic_id); panels = json.loads(comic.panels_content)
    return render_template('view_comic.html', panels=panels, title=comic.story.title, comic_id=comic.id)

# --- OTHER ROUTES ---
@app.route('/styles')
def styles_page(): return render_template('manage_styles.html', styles=Style.query.all())
@app.route('/add-style', methods=['POST'])
def add_style(): db.session.add(Style(name=request.form['style_name'], content=request.form['style_content'])); db.session.commit(); return redirect(url_for('styles_page'))
@app.route('/delete-style', methods=['POST'])
def delete_style(): s = Style.query.filter_by(name=request.form['style_to_delete']).first(); db.session.delete(s) if s else None; db.session.commit(); return redirect(url_for('styles_page'))
@app.route('/saved-stories')
def saved_stories_page(): return render_template('saved_stories.html', stories=Story.query.order_by(Story.id.desc()).all())

@app.route('/save-story', methods=['POST'])
def handle_save_story():
    content = request.form.get('story_content', ''); title = "Untitled"; first_line = content.strip().split('\n')[0]
    if "#" in first_line: title = first_line.replace('#', '').strip()
    db.session.add(Story(title=title, content=content)); db.session.commit(); return redirect(url_for('saved_stories_page'))

@app.route('/delete-story', methods=['POST'])
def handle_delete_story(): s = Story.query.get(request.form.get('story_id')); db.session.delete(s) if s else None; db.session.commit(); return redirect(url_for('saved_stories_page'))

@app.route('/edit-story/<int:story_id>', methods=['GET', 'POST'])
def edit_story_page(story_id):
    s = Story.query.get_or_404(story_id)
    if request.method == 'POST': s.title = request.form['title']; s.content = request.form['content']; db.session.commit(); return redirect(url_for('saved_stories_page'))
    return render_template('edit_story.html', story=s)

@app.route('/translate-story')
def translate_page(): return render_template('translate_story.html')

@app.route('/handle-translation', methods=['POST'])
def handle_translation():
    api_key = configure_ai()
    if not api_key: return jsonify({"story_result": "ERROR: API Key missing."}), 500
    data = request.form
    inputs = {"folktale_name": data.get('folktale_name'), "level": data.get('cefr_level'), "count": data.get('word_count'), "target_audience": data.get('target_audience')}
    prompt = create_translation_prompt(inputs)
    return jsonify({"story_result": generate_story(api_key, prompt)})

@app.route('/add-quiz-to-saved', methods=['POST'])
def add_quiz_to_saved():
    story = Story.query.get(request.form.get('story_id'))
    quiz_type = request.form.get('quiz_type')
    api_key = configure_ai()
    if story and api_key and quiz_type:
        try:
            prompt = create_quiz_only_prompt(story.content, quiz_type)
            quiz_content = generate_story(api_key, prompt)
            story.content += f"\n\n--- EXTRA QUIZ ({quiz_type.upper()}) ---\n{quiz_content}"
            db.session.commit()
            flash(f"Added {quiz_type} quiz!", "success")
        except Exception as e:
            flash(f"Error creating quiz: {e}", "danger")
    return redirect(url_for('saved_stories_page'))

# --- TỰ ĐỘNG TẠO BẢNG KHI IMPORT APP ---
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    # Đoạn này để chạy dưới máy tính (Local)
    if os.environ.get('WERKZEUG_RUN_MAIN') != 'true':
        webbrowser.open_new('http://127.0.0.1:5000/')
    app.run(debug=True, port=5000)