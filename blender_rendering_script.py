import bpy
import random
import math
import numpy as np
import mathutils
import os
import json 

from scipy.stats import qmc

# Queue of random samples from Sobol sequence
qmc_samples = []
sampler = qmc.Sobol(d=2, scramble=False)
samples = sampler.random_base2(m=8)
for sample in samples:
    print(sample)
    qmc_samples.append((sample[0], sample[1]))

'''
Orients a camera object to look at a particular point in space.
reference: https://blender.stackexchange.com/questions/5210/pointing-the-camera-in-a-particular-direction-programmatically
'''
def look_at(obj_camera, point):
    loc_camera = obj_camera.matrix_world.to_translation()

    direction = point - loc_camera
    # point the cameras '-Z' and use its 'Y' as up
    rot_quat = direction.to_track_quat('-Z', 'Y')

    # assume we're using euler rotation
    obj_camera.rotation_euler = rot_quat.to_euler()

'''
Uniformly samples the surface of unit hemisphere centered at (0,0,0)
pointing upward in the +Z direction.
'''
def uniformSampleHemisphere():
    u1, u2 = qmc_samples.pop(0)
    z = u1
    r = math.sqrt(max(0.0, 1.0 - z ** 2))
    phi = 2 * math.pi * u2
    return np.array([r * math.cos(phi), r * math.sin(phi), z])

'''
Samples a new camera location and adjusts the camera orientation
'''
def randomlyMoveCamera(camera_obj):
    # Sample hemisphere point
    loc = uniformSampleHemisphere()
    
    # Move camera to loc
    camera_obj.location = mathutils.Vector(loc)
    
    # Update internal Blender matrices, IMPORTANT!
    bpy.context.view_layer.update()
    
    # Orient camera to look at origin
    look_at(camera_obj, mathutils.Vector((0,0,0)))

'''
Renders variable number of random view. Saves images and transforms to output_dir.
'''
def renderRandomViews(output_dir='./', num_views=2, file_dir='train'):
    light_min = 1.0
    light_max = 10.0
    
    light_pos_radius = 0.25
    
    my_camera = bpy.data.objects["TEST_CAMERA"]    
    my_light = bpy.data.lights["Sun"]
    my_point_light = bpy.data.objects["POINT_LIGHT"]

    mat = bpy.data.materials["SphereMat"]
    principled = mat.node_tree.nodes["Principled BSDF"]
    
    print('Camera angle_x: ', bpy.context.scene.camera.data.angle_x)
    output_dict = {
        'camera_angle_x': bpy.context.scene.camera.data.angle_x,
        'frames': []
    }
    
    for i in range(num_views):
        print('Rendering view ', i)
        randomlyMoveCamera(my_camera)
#        my_light.energy = random.uniform(light_min, light_max)
#        red = random.uniform(0,1)
#        blue = random.uniform(0,1)
#        green = random.uniform(0,1)
#        principled.inputs["Base Color"].default_value = (red, blue, green, 1.0)
        
        rand_metallic = random.uniform(0.0, 1.0)
        principled.inputs['Metallic'].default_value = rand_metallic
#        t = random.uniform(0, 2 * 3.1415926)
#        light_x = 0.25 * np.cos(t)
#        light_y = 0.25 * np.sin(t)
#        light_z = 0.25
#        
#        my_point_light.location = mathutils.Vector(np.array([light_x, light_y, light_z]))

        filename = f'r_{i}.png'
        bpy.context.scene.render.filepath = os.path.join(output_dir, filename)
        bpy.ops.render.render(write_still = True)
        trans_mat = my_camera.matrix_world
        output_dict['frames'].append({
            'file_path': f'./{file_dir}/r_{i}',
            'transform_matrix': [
                list(trans_mat[0]),
                list(trans_mat[1]),
                list(trans_mat[2]),
                list(trans_mat[3]),
            ],
            'metallic': rand_metallic
#            'diffuse': [
#                red,
#                green,
#                blue
#            ]
#            'light_pos': [
#                light_x,
#                light_y,
#                light_z
#            ]
#            'light_intensity': my_light.energy
        })
    
    json_path = os.path.join(output_dir, f'transforms_{file_dir}.json')
#    json_path = f'transforms_{file_dir}.json'
    with open(json_path, 'w') as outfile:
        json.dump(output_dict, outfile, indent=2)    


# Render train views
renderRandomViews(
    output_dir='C:\\Users\\Tank\\Documents\\Brown\\Courses\\Graphics\\CSCI2240-Final\\blender_scenes\\metal1\\train',
    num_views=128,
    file_dir='train'
)

# Render test views
renderRandomViews(
    output_dir='C:\\Users\\Tank\\Documents\\Brown\\Courses\\Graphics\\CSCI2240-Final\\blender_scenes\\metal1\\test',
    num_views=16,
    file_dir='test'
)