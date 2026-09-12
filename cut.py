import ffmpeg

input_file = "C:\GhostToolbox\Antonia Sainz X Ria Sunn X Stracy Stone VR (2160).mp4"   # Your original video
output_file = "output.mp4" # New 10-min video
target_duration = 10 * 60  # 10 minutes in seconds

# Get the duration of the input video
probe = ffmpeg.probe(input_file)
input_duration = float(probe['format']['duration'])

if input_duration >= target_duration:
    # If the input is longer than 10 min, just trim
    (
        ffmpeg
        .input(input_file, t=target_duration)
        .output(output_file, c='copy')  # copy streams without re-encoding for speed
        .run(overwrite_output=True)
    )
else:
    # If the input is shorter than 10 min, loop it
    loop_count = int(target_duration // input_duration) + 1
    # Use concat demuxer method for speed
    with open("files.txt", "w") as f:
        for _ in range(loop_count):
            f.write(f"file '{input_file}'\n")

    (
        ffmpeg
        .input("files.txt", format='concat', safe=0)
        .output(output_file, t=target_duration, c='copy')
        .run(overwrite_output=True)
    )

print("Done! Created 10-minute video:", output_file)