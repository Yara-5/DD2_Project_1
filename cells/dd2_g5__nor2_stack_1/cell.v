module dd2_g5__nor2_stack_1 (A, B, Y);
  input A;
  input B;
  output Y;
  assign Y = ~(A | B);
endmodule
