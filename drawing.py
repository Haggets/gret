from gpu_extras.batch import batch_for_shader
# import bgl
import base64
import blf
import bpy
import gpu
import numpy as np

shader_image = gpu.shader.from_builtin('2D_IMAGE')
shader_solid = gpu.shader.from_builtin('2D_UNIFORM_COLOR')
h1_font_size = 19
p_font_size = 15
line_height_h1 = int(1.3 * h1_font_size)
line_height_p = int(1.3 * p_font_size)
font_id = 0
rect_texcoords = (0, 0), (1, 0), (1, 1), (0, 1)

class UVSheetTheme:
    unselectable = (0.8, 0.8, 0.8, 0.7) # (0.6, 0.6, 0.6, 0.6)
    unselected = (0.8, 0.8, 0.8, 0.7)
    hovered = (1.0, 1.0, 1.0, 1.0)
    selected = (0.4, 1.0, 0.2, 1.0)
    bad = (1.0, 0.2, 0.2, 1.0)
    grid = (0.8, 0.8, 0.8, 0.2)
    marquee = (1.0, 1.0, 1.0, 1.0)
    border = (0.42, 0.42, 0.42, 1.0)
    point_size = 6.0

    @property
    def background(self):
        return bpy.context.preferences.themes[0].image_editor.space.back

    @property
    def selected(self):
        return bpy.context.preferences.themes[0].image_editor.edge_select

icon_b64 = {
    'HELP': r"////AP///wD///8A////AP///wD///8n////cP///5T///+W////dv///zP///8A////AP///wD///8A////AP///wD///8A////AP///yP///+0////////////////////////////////////xv///zD///8A////AP///wD///8A////AP///zr////v///////////////2////V////0T////m///////////////4////VP///wD///8A////AP///x3////t////////////////////tP///wD///8A////j/////////////////////n///80////AP///wD///+p/////////////////////////+7///81////Iv///9n/////////////////////////y////wH///8Y/////P/////////////////////////5////k////5P////t//////////////////////////////84////W///////////////////////////////8v///wD///8A////Z////+j/////////////////////////fv///3z///////////////////////////////3///8g////AP///wD///8b////4v///////////////////5////97////////////////////////////////////4v///43///8V////AP///1z///////////////////+e////Wf/////////////////////////9////9P//////////////pv///wD///8Z////////////////////fP///xb////7//////////////+H////Ff///23//////////////5T///8A////IP///////////////////zT///8A////o///////////////nP///wD///8C////Vf///17///8G////AP///3L//////////////8b///8A////AP///xn////p//////////z///9P////AP///wD///8A////AP///zj////z//////////f///8u////AP///wD///8A////NP///+v//////////f///6j///9i////X////6D////6//////////X///9L////AP///wD///8A////AP///wD///8c////qf////3//////////////////////////////7z///8p////AP///wD///8A////AP///wD///8A////AP///wD///8e////Zv///4n///+M////bP///yn///8A////AP///wD///8A////AA==",
    'RESIZE': r"////BP///1////9t////bf///23///9t////bf///xH///8A////AP///wD///8A////AP///wD///8A////AP///2H///////////////////////////////////8n////AP///wD///8A////AP///wD///8A////AP///wD///9w////////////////////7////9z////c////Iv///wD///8A////AP///wD///8A////AP///wD///8A////cP///////////////////+z///8v////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///3D/////////7v///+z/////////6////y7///8A////AP///wD///8A////AP///wD///8A////AP///wD///9w/////////9j///8w////7P/////////r////MP///wD///8A////AP///wD///8A////AP///wD///8A////cP/////////Y////AP///y/////s/////////+z///8w////AP///wD///8A////AP///wD///8A////AP///xP///8r////Jf///wD///8A////MP///+3/////////7P///y////8A////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///wD///8x////6//////////r////Lv///wD///8A////Iv///yf///8R////AP///wD///8A////AP///wD///8A////AP///y/////r/////////+v///8w////AP///97/////////cP///wD///8A////AP///wD///8A////AP///wD///8A////Lv///+v/////////7P///zD////e/////////3D///8A////AP///wD///8A////AP///wD///8A////AP///wD///8v////7P/////////s////8P////////9w////AP///wD///8A////AP///wD///8A////AP///wD///8A////AP///zD////q////////////////////cP///wD///8A////AP///wD///8A////AP///wD///8A////Iv///+D////g////8f///////////////////3D///8A////AP///wD///8A////AP///wD///8A////AP///yf///////////////////////////////////9h////AP///wD///8A////AP///wD///8A////AP///wD///8R////bf///23///9t////bf///23///9d////BA==",
}
icon_textures = {}
icon_size = (16, 16)

def image_to_base64(image_name):
    image = bpy.data.images[image_name]
    data = np.array(image.pixels) * np.iinfo(np.uint8).max  # Scale to [0..255]
    data = data.astype(np.int8).tobytes()
    return base64.b64encode(data)
# bpy.context.window_manager.clipboard = image_to_base64("image.png")

def base64_to_pixels(s):
    if not s:
        return None
    data = base64.b64decode(s)
    data = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
    data /= np.iinfo(np.uint8).max  # Scale to [0..1]
    return data

def get_icon(icon_id):
    texture = icon_textures.get(icon_id)
    if not texture:
        data = base64_to_pixels(icon_b64.get(icon_id))
        data_len = icon_size[0] * icon_size[1] * 4
        if data is None or len(data) != data_len:
            data = np.ones(data_len, dtype=np.float32)
        buf = gpu.types.Buffer('FLOAT', len(data), data)
        texture = gpu.types.GPUTexture(icon_size, format='RGBA32F', data=buf)
        icon_textures[icon_id] = texture
    return texture

def draw_image(x0, y0, x1, y1, image, nearest=False):
    if not image:
        return
    # XXX Filters not exposed in gpu module, workaround with bgl is simple enough however the state
    # is messed up afterwards, causing other things to break.
    nearest = False
    if nearest:
        image.gl_load()
        shader_image.bind()
        bgl.glEnable(bgl.GL_BLEND)
        bgl.glEnable(bgl.GL_TEXTURE_2D)
        bgl.glBindTexture(bgl.GL_TEXTURE_2D, image.bindcode)
        bgl.glTexParameteri(bgl.GL_TEXTURE_2D, bgl.GL_TEXTURE_MIN_FILTER, bgl.GL_NEAREST)
        bgl.glTexParameteri(bgl.GL_TEXTURE_2D, bgl.GL_TEXTURE_MAG_FILTER, bgl.GL_NEAREST)
    else:
        texture = gpu.texture.from_image(image)
        shader_image.bind()
        shader_image.uniform_sampler("image", texture)
    batch_for_shader(shader_image, 'TRI_FAN', {
        "pos": ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        "texCoord": rect_texcoords,
    }).draw(shader_image)
    if nearest:
        bgl.glDisable(bgl.GL_TEXTURE_2D)
        image.gl_free()

def draw_icon(x0, y0, x1, y1, icon_id, color):
    # TODO: image color shader...
    texture = get_icon(icon_id)
    if not texture:
        return
    shader_image.bind()
    shader_image.uniform_sampler("image", texture)
    gpu.state.blend_set('ALPHA')
    batch_for_shader(shader_image, 'TRI_FAN', {
        "pos": ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
        "texCoord": rect_texcoords,
    }).draw(shader_image)
    gpu.state.blend_set('NONE')

def draw_point(x, y, color, size=1.0):
    if len(color) == 3:
        color = *color, 1.0
    use_blend = color[3] < 1.0
    shader_solid.bind()
    shader_solid.uniform_float("color", color)
    if use_blend:
        gpu.state.blend_set('ALPHA')
    gpu.state.point_size_set(size)
    batch_for_shader(shader_solid, 'POINTS', {
        "pos": [(x, y)],
    }).draw(shader_solid)
    if use_blend:
        gpu.state.blend_set('NONE')

def draw_box_fill(x0, y0, x1, y1, color):
    if len(color) == 3:
        color = *color, 1.0
    use_blend = color[3] < 1.0
    shader_solid.bind()
    shader_solid.uniform_float("color", color)
    if use_blend:
        gpu.state.blend_set('ALPHA')
    batch_for_shader(shader_solid, 'TRI_FAN', {
        "pos": ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
    }).draw(shader_solid)
    if use_blend:
        gpu.state.blend_set('NONE')

def draw_box(x0, y0, x1, y1, color, width=1.0):
    if len(color) == 3:
        color = *color, 1.0
    use_blend = color[3] < 1.0
    shader_solid.bind()
    shader_solid.uniform_float("color", color)
    if use_blend:
        gpu.state.blend_set('ALPHA')
    gpu.state.line_width_set(width)
    batch_for_shader(shader_solid, 'LINE_LOOP', {
        "pos": ((x0, y0), (x1, y0), (x1, y1), (x0, y1)),
    }).draw(shader_solid)
    if use_blend:
        gpu.state.blend_set('NONE')

def draw_grid(x, y, grid_width, grid_height, num_cols, num_rows, color, width=1.0):
    if len(color) == 3:
        color = *color, 1.0
    use_blend = color[3] < 1.0
    shader_solid.bind()
    shader_solid.uniform_float("color", color)
    x0, y0 = x, y
    x1, y1 = x + grid_width * num_cols, y + grid_height * num_rows
    lines = []
    for n in range(num_cols + 1):
        x = x0 + n * grid_width
        lines.extend(((x, y0), (x, y1)))
    for n in range(num_rows + 1):
        y = y0 + n * grid_height
        lines.extend(((x0, y), (x1, y)))
    gpu.state.line_width_set(width)
    if use_blend:
        gpu.state.blend_set('ALPHA')
    batch_for_shader(shader_solid, 'LINES', {
        "pos": lines,
    }).draw(shader_solid)
    if use_blend:
        gpu.state.blend_set('NONE')

def draw_help_box(x0, y0, texts, title="", padding=16.0, width=None):
    if not texts and not title:
        return

    # Guess dimensions
    if width is None:
        width = max(len(s) for s in texts) * p_font_size * 0.5
        width = max(width, len(title) * h1_font_size * 0.5)
    width += padding * 2.0

    height = len(texts) * line_height_p
    if title:
        if texts:
            height += 8.0
        height += line_height_h1
    height += padding * 1.75

    # Draw background
    draw_box_fill(x0, y0, x0 + width, y0 + height, (0.0, 0.0, 0.0, 0.5))

    # Draw text starting from the bottom
    y = y0 + padding + line_height_p * 0.25
    blf.color(font_id, 1.0, 1.0, 1.0, 1.0)

    for text in reversed(texts):
        blf.size(font_id, p_font_size, 60)
        blf.position(font_id, x0 + padding, y, 0)
        blf.draw(font_id, text)
        y += line_height_p

    if title:
        if texts:
            y += 8.0
        blf.size(font_id, h1_font_size, 60)
        blf.position(font_id, x0 + padding, y, 0)
        blf.draw(font_id, title)
