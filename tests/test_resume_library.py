from jobagent.tools.resume_library import ResumeLibrary


def test_register_and_list_pdf_resume(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\nresume")
    library = ResumeLibrary(tmp_path / "resumes")
    assert library.register(str(source))["status"] == "registered"
    listed = library.list()["resumes"]
    assert listed[0]["file_name"] == "source.pdf"
    assert len(listed[0]["sha256"]) == 64
