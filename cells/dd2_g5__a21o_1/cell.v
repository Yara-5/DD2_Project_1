module dd2_g5__a21o_1 (A1, A2, B1, X);
  input A1;
  input A2;
  input B1;
  output X;
  assign X = (A1 & A2) | B1;
endmodule
