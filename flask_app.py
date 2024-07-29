from flask import (Flask, request, render_template, Response, stream_with_context,
    redirect, session, url_for, send_from_directory, send_file)
from authlib.integrations.flask_client import OAuth
import sys
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
import anthropic # had to pip install this
from time import sleep
from filelock import FileLock
from datetime import datetime
from zoneinfo import ZoneInfo
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
from textract import process as process_upload # had to pip install this; still needs to be tested on .doc
from markdown import markdown as markdown_to_html # had to pip install this
from weasyprint import HTML as weasy_html # failed on replit but works on PythonAnywhere
import openai
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, FuncFormatter
from io import BytesIO

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app) # convert 10.x.x.x to real visitor IPs
app.secret_key = ''

#login_manager = LoginManager()
#login_manager.init_app(app)

oauth = OAuth(app)
linkedin = oauth.register(
    name='linkedin',
    client_id='86qk5c0nj2enqf',
    client_secret='90EyKvsHtxawSy9c',
    redirect_uri='https://www.applyto.jobs/loggedin',
    authorize_url='https://www.linkedin.com/oauth/v2/authorization',
    #authorize_params=None,
    access_token_url='https://www.linkedin.com/oauth/v2/accessToken',
    access_token_method='POST',
    refresh_token_url=None,
    #redirect_to=None,
    client_kwargs={'scope': 'profile email'}  #was also: w_member_social  # specify the scopes you want
)


# Get a response from the Anthropic Claude LLM
def claude(prompt): # see https://github.com/anthropics/anthropic-sdk-python

    llm = anthropic.Client(api_key=
        '')

    for attempt in range(20): # retry rate limits and server overloads twenty times
        try:
            result = llm.completions.create(temperature=0.0, # 0 for quasi-reproducibility
                max_tokens_to_sample=5000, # maximum response tokens, ~20,000 words
                prompt=f'{anthropic.HUMAN_PROMPT} {prompt}{anthropic.AI_PROMPT}',
                stop_sequences=[anthropic.HUMAN_PROMPT], model='claude-v1.3') # NEW! claude-2
                # see https://docs.anthropic.com/claude/reference/complete_post

            return result.completion.strip() # return Claude's response

        except anthropic.RateLimitError as err: # not yet encountered even when simultanious
            print(f"!!! Claude API error {err.status_code}: {err.response.text}, "
                   "retrying {19 - attempt} times....")
        except anthropic.InternalServerError as err: # i.e. 529: temporarily overloaded
            print(f"!!! Claude API error {err.status_code}: {err.response.text}, "
                   "retrying {19 - attempt} times....") # also haven't ever seen yet
        sleep(2) # pause for rate limits and overloadings; everything else is uncaught

    # If all retry attempts are exhausted, raise RuntimeError exception to halt
    raise RuntimeError("Oops! Something went wrong while we were trying to complete"
                       " your action. Don't worry, it's not your fault. Please try"
                       " refreshing the page or check back in a few minutes. If the"
                       " issue persists, contact our support team for help. We "
                       " apologize for the inconvenience.")

resume_prompt = '''Based on the provided resume and job description, tailor a
polished resume to showcase the candidate's skills, experiences, and qualifications
that are relevant to the desired position. Make any assumptions necessary to help
the applicant's resume stand out, by having it include transferable skills and keywords that match
the job description. Ensure the resume is professionally formatted and Applicant Tracking System-compliant,
grammatically correct, and tailored to increase the candidate's chances of
landing the job. Do not include the job description in the revised resume. Do not include any information
on the tailored resume unless it is evident on the original.'''

resume_prompt_original = '''Given the following resume and job description, create a
well-crafted resume that effectively highlights the user's skills, experiences,
and qualifications relevant to the desired position. It should be professionally
formatted and visually appealing. Please ensure that the generated resume is
grammatically accurate, and enables the user to effectively showcase their
abilities and enhance their chances of securing the desired job opportunity. Do NOT
include the job description position in the resume.'''

resume_difference = '''Describe the changes that were made from the following original resume to the
tailored resume. Briefly include why the changes are beneficial.'''

coverletter_prompt = '''You have access to a job description and the user's
resume. Your task is to generate a well-crafted cover letter of LENGTH.
Please start with a brief  opening paragraph to introduce the user and express
their interest in the position. Mention the job title and reference specific
skills and achievements from the user's resume that align with the job
requirements. Demonstrate knowledge of the company and its values, and explain
why the user is excited about the specific company and that particular position.
Convey enthusiasm and eagerness to contribute to the company's success,
including examples of how the applicant would be able to do so.  Do not include
any information on the cover letter that is not already written on the resume.
End the cover letter with a polite closing and sign it with the the user's
contact information as provided in the resume.'''

skills_prompt = '''Create a raw HTML table featuring four columns: "Skill Gap," "Action Plan,"
"Course/Certificate Recommendations," and "Priority Level." In the "Skill Gap" column,
enumerate the skills or experiences from the job description that are not evident in the provided resume.
For "Action Plan," suggest methods for acquiring these missing skills or experiences starting from zero knowledge.
In "Course/Certificate Recommendations," offer names or links to free courses or certificates from platforms
such as Coursera or Udemy that can aid in skill acquisition. In "Priority Level," rank each skill or experience's importance
on a scale from 1 to 5, where 5 is essential and 1 is optional. Ensure that the table features bold and centered headings, as well as 1-pixel collapsed border lines.'''

linkedin_prompt = '''Please create a concise and engaging Linkedin summary of
1000 characters. It should emphasize the most impressive aspects of the user's
resume. Highlight relevant skills, achievements, and experiences that align with
the desired position. Focus on presenting a compelling narrative that grabs the
attention of potential employers and compels them to learn more about the user's
qualifications. The summary should not be specific to the job description, and
should be versatile enough that it can be used for different job descriptions
of a position within the same field or industry. '''

prompt_suffix = '''\n
[RESUME:]
%s
[END RESUME]

[JOB DESCRIPTION:]
%s
[END JOB DESCRIPTION]\n'''

search_prompt = '''Write a list of search terms for job posting search sites to
find openings most closely matching the following resume:

%s\n'''

@app.route('/', methods=['GET', 'POST'])
def index():
    #return 'comment this line out to turn back on'

    # show how many submissions so far
    lock = FileLock('.counter.lock')
    with lock:
        with open('mysite/counter.txt', 'r') as f:
            counter = f.read().strip()

    if request.method == 'GET':
        return render_template('landingform.html', served=counter, resume='')

    # method == POST
    resumetext = ''

    if 'file' in request.files and request.files['file'].filename != '': # upload from original landingform.html
        # get resume from upload button
        file = request.files['file']
        filename = ('runs/upload-' + datetime.now(ZoneInfo('US/Pacific')
                                      ).strftime('%Y-%m-%d-%H-%M-%S.%f')[:-3] # microseconds to milliseconds
                    + '-' + secure_filename(file.filename))
        file.save(filename)
        resumetext = process_upload(filename).decode('utf-8') # textract.process()

        return render_template('landingform.html', served=counter, resume=resumetext)

    if 'file-upload' in request.files and request.files['file-upload'].filename != '':  #upload from newlanding.html
        # get resume from upload button
        file = request.files['file-upload']
        filename = ('runs/upload-' + datetime.now(ZoneInfo('US/Pacific')
                                      ).strftime('%Y-%m-%d-%H-%M-%S.%f')[:-3] # microseconds to milliseconds
                    + '-' + secure_filename(file.filename))
        file.save(filename)
        resumetext = process_upload(filename).decode('utf-8') # textract.process()

    # regular POST form submission

    with lock:
        with open('mysite/counter.txt', 'w') as f:
            f.write(str(int(counter) + 1) + '\n') # increment counter

    # this allows for progressive updates
    def generate():

        # get the form inputs
        if resumetext == '':
            resume = request.form.get('resume')
        else:
            resume = resumetext

        jobdescription = request.form.get('jobdescription')
        lettersize = request.form.get('length')

        spinner = render_template('spinner.html') # template has a %s in it

        yield render_template('present.html') + '\n\n' + spinner % ('resume')
                #+ ' (please be patient, each of these sections take about 15-20 seconds)'


        tailored_resume = claude(resume_prompt + prompt_suffix % (resume, jobdescription))

        yield ('<h2>Resume: (Warning: Please proofread for correctness)</h2>\n'
               + '<p id="resume" style="white-space: pre-wrap;">'
               + tailored_resume + '</p>\n\n' + render_template('download.html')
               + spinner % 'differences from your original')

        differences = claude(resume_difference + '\n\n[ORIGINAL RESUME:]\n' +
                resume + '\n\n[TAILORED RESUME:]\n' + tailored_resume)

        yield ('<h2>Changes made to resume: </h2>\n'
               + '<p id="resume" style="white-space: pre-wrap;">'
               + differences + '</p>\n\n'+ spinner % 'cover letter')

        # coverletter_prompt = substitution(based on the lettersize)
        if lettersize == 'short':
            letter_prompt = coverletter_prompt.replace('LENGTH', '200 word')
        if lettersize == 'medium':
            letter_prompt = coverletter_prompt.replace('LENGTH', '325 word')
        if lettersize == 'long':
            letter_prompt = coverletter_prompt.replace('LENGTH', '400 word')

        coverletter = claude(letter_prompt + prompt_suffix % (resume, jobdescription))

        yield ('<h2>Cover letter:</h2>\n<p style="white-space: pre-wrap;">'
               + coverletter + '</p>\n\n' + spinner % 'skills advice')


        skills_advice = claude(skills_prompt + prompt_suffix % (resume, jobdescription))

        yield ('<h2>Skills advice:</h2>\n' + skills_advice + '\n\n'
               + spinner % 'LinkedIn summary')


        linkedin_summary = claude(linkedin_prompt + prompt_suffix % (resume, jobdescription))

        yield ('<h2>LinkedIn summary:</h2>\n<p style="white-space: pre-wrap;">'
               + linkedin_summary + '</p>\n\n') #+ spinner % 'search terms')


        #search_terms = claude(search_prompt % resume)
        #
        #yield ('<h2>Recommended job search terms:</h2>\n<p style="white-space: '
        #       + 'pre-wrap;">' + search_terms + '</p>\n'
        #       + render_template("closing.html"))

        yield (render_template("closing.html"))

        # store data for debugging
        fn = 'run-' + datetime.now(ZoneInfo('US/Pacific')
                                  ).strftime('%Y-%m-%d-%H-%M-%S.%f')[:-3] + '.txt'
        with open('runs/' + fn, 'w') as f:
            f.write(fn + ' from ' + request.remote_addr
                    + '\n\n### resume:\n\n' + resume
                    + '\n\n### jobdescription:\n\n' + jobdescription
                    + '\n\n### tailored_resume:\n\n' + tailored_resume
                    + '\n\n### coverletter:\n\n' + coverletter
                    + '\n\n### skills_advice:\n\n' + skills_advice
                    + '\n\n### linkedin_summary:\n\n' + linkedin_summary + '\n\n')
                    #+ '\n\n### search_terms:\n\n' + search_terms + '\n\n')

    # progressively send the web page
    response = Response(stream_with_context(generate()), mimetype='text/html')
    response.headers['X-Accel-Buffering'] = 'no' # disable PythonAnywhere's buffering
    return response

@app.route('/downloadresume', methods=['POST'], defaults={'fn': ''})
@app.route('/downloadresume/<path:fn>', methods=['POST'])
def downloadresume(fn):
    if 'resumetext' in request.form:
        resume = request.form.get('resumetext')

        markdown = claude('Format the following resume in markdown. '
                          + 'Respond only with its raw markdown text, with '
                          + 'the name as a level 1 "# " heading:\n\n'
                          + resume).strip()

        return render_template('proofread.html', resume=markdown)

    if 'markdown' in request.form:
        filename = request.form.get('filename')
        if filename == '':
            filename = 'resume'
        else:
            filename = secure_filename(filename)

        html = markdown_to_html(request.form.get('markdown')) # markdown.markdown()

        html = '<style>body { font-size: 13px; }</style>\n' + html # smaller fonts

        pdf = weasy_html(string=html).write_pdf() # weasyprint.HTML()

        response = Response(pdf, mimetype='application/pdf')
        response.headers['Content-Disposition'] = f'inline; filename={filename}.pdf'
                                                  # attachment to download, inline to display
        return response

    return redirect('/')

@app.route('/privacypolicy') # Privacy Policy and Release Notes
def privacypolicy():
    return render_template('privacypolicy.html')

@app.route('/robots.txt') # tell search engines to only index the front page and graphics
def robots():
    return Response('User-agent: *\nDisallow: /\nAllow: /$\nAllow: /graphics/\nAllow: /privacypolicy\n',
                    mimetype='text/plain')

@app.route('/helpwanted') # for the hackathon etc.
def helpwanted():

    # show how many submissions so far
    lock = FileLock('.counter.lock')
    with lock:
        with open('mysite/counter.txt', 'r') as f:
            counter = f.read().strip()

    return render_template('helpwanted.html', served=counter)

@app.route('/new')
def newlanding():

    # show how many submissions so far
    lock = FileLock('.counter.lock')
    with lock:
        with open('mysite/counter.txt', 'r') as f:
            counter = f.read().strip()

    return render_template('newlanding.html', served=counter)


@app.route('/ls.png')  # Shanaya: I added these to track visits to your mom's site
def track_laughterseriously():
    ipaddr = request.remote_addr
    timestamp = datetime.now(ZoneInfo('US/Pacific')).strftime('%Y %m %d %H %M %S: ')
    with open('laughterseriouslylog.txt', 'a') as f:
        f.write(timestamp + ipaddr + '\n')
    return send_from_directory('graphics', 'transparent-pixel.png', mimetype='image/png')

@app.route('/lsgraph')
def graph_accesses():
    with open('laughterseriouslylog.txt', 'r') as f:
        lines = f.readlines()
    times = [datetime.strptime(line.split(':')[0], '%Y %m %d %H %M %S') for line in lines]
    now = datetime.now() + datetime.now(ZoneInfo('US/Pacific')).utcoffset()

    plt.figure(figsize=(6, 7))
    ax1 = plt.subplot(2, 1, 1)
    plt.title('LaughterSeriously.com accesses')
    plt.plot(times, range(len(times)))  # line graph
    ax1.set_xlim([min(times), now])
    ax1.set_ylim([1, len(times)])
    ax1.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda x, _: '{:,.0f}'.format(x)))
    plt.setp(ax1.get_xticklabels(), visible=False)

    ax2 = plt.subplot(2, 1, 2)
    plt.hist(times, bins=45, rwidth=0.8)  # histogram
    ax2.set_xlim([min(times), now])
    ax2.yaxis.set_major_locator(MaxNLocator(integer=True))
    plt.xticks(rotation=45)
    plt.tight_layout()

    img = BytesIO()  # Convert plot to PNG image
    plt.savefig(img, format="png")
    img.seek(0)
    response = send_file(img, mimetype='image/png')
    response.headers["Content-Disposition"] = "inline; filename=lsgraph.png"
    return response


@app.route('/linkedin')
def linkedin_login():
    print ('*** Asking LinkedIn to log in', file=sys.stderr)
    redirect_uri = url_for('authorize_linkedin', _external=True)
    return linkedin.authorize_redirect(redirect_uri)

@app.route('/linkedin-authorized')
def authorize_linkedin():
    print ('*** Loging in with LinkedIn:', vars(linkedin), file=sys.stderr)
    token = linkedin.authorize_access_token()
    resp = linkedin.get('https://api.linkedin.com/v2/me')
    profile = resp.json()
    # Usually, you'd store the user's profile in your database and set a cookie to remember the user.
    user_id = profile["id"]  # get user id from the LinkedIn response
    #user = User(user_id)
    #login_user(user)
    print ('** Logged in with LinkedIn: ' + user_id + '\n Profile: ' + repr(profile), file=sys.stderr)
    return 'Logged in as ' + user_id

